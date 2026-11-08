"""Bounded, profile-local QQ image inputs. Nothing here writes media to disk.

Only URLs supplied by trusted QQ image segments may reach ``prepare``. Scope,
member permission and profile checks belong to the caller. The URL allowlist
and decoder remain mandatory even after those checks. Signed CDN URLs are
never returned as metadata, exception messages or model inputs.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import hashlib
import io
import time
from urllib.parse import urlsplit
import uuid
import warnings

import httpx
from PIL import Image, ImageOps


MAX_IMAGES = 3
MAX_DOWNLOAD_BYTES = 6 * 1024 * 1024
MAX_IMAGE_BYTES = 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
MAX_DIMENSION = 8192
NORMALIZED_DIMENSION = 1536
QQ_IMAGE_HOSTS = frozenset({"multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn"})


class VisionError(ValueError):
    """Only fixed codes may cross the image boundary."""


def validate_url(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in value) or "\\" in value):
        raise VisionError("vision_url_rejected")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in QQ_IMAGE_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.fragment):
            raise ValueError()
    except (ValueError, TypeError):
        raise VisionError("vision_url_rejected") from None
    return value


def normalize_image(data: bytes) -> dict:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_DOWNLOAD_BYTES:
        raise VisionError("vision_image_too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    raise VisionError("vision_format_unsupported")
                width, height = source.size
                if (not 0 < width <= MAX_DIMENSION or not 0 < height <= MAX_DIMENSION
                        or width * height > MAX_PIXELS):
                    raise VisionError("vision_dimensions_exceeded")
                animated = getattr(source, "n_frames", 1) > 1
                original_format = source.format
                source.seek(0)
                frame = ImageOps.exif_transpose(source).convert("RGBA")
                frame.thumbnail((NORMALIZED_DIMENSION, NORMALIZED_DIMENSION), Image.Resampling.LANCZOS)
                # Rebuild pixels into a fresh image, removing EXIF, comments,
                # embedded profiles and other metadata, including signed URLs.
                clean = Image.new("RGB", frame.size, "white")
                clean.paste(frame, mask=frame.getchannel("A"))
                out = io.BytesIO()
                mime = "image/png"
                if original_format == "PNG":
                    clean.save(out, format="PNG")
                if original_format != "PNG" or out.tell() > MAX_IMAGE_BYTES:
                    out = io.BytesIO()
                    clean.save(out, format="JPEG", quality=85, optimize=True)
                    mime = "image/jpeg"
                if out.tell() > MAX_IMAGE_BYTES:
                    out = io.BytesIO()
                    clean.save(out, format="JPEG", quality=65, optimize=True)
                normalized = out.getvalue()
                if not normalized or len(normalized) > MAX_IMAGE_BYTES:
                    raise VisionError("vision_image_too_large")
                return {"data": normalized, "mime_type": mime, "width": clean.width,
                        "height": clean.height, "animated_first_frame": animated,
                        "sha256": hashlib.sha256(normalized).hexdigest()}
    except VisionError:
        raise
    except Exception:
        raise VisionError("vision_image_invalid") from None


class VisionCache:
    """One instance per profile; bounded memory and no cross-scope lookups.

    Failed inputs remain explicit unavailable records for the same short TTL.
    Deleting a scope/member invalidates in-flight work before it can commit.
    At most two HTTP/decode jobs run and at most 32 events wait for a slot.
    """

    def __init__(self, *, clock=time.monotonic, transport=None, ttl_seconds=1200,
                 max_bytes=24 * 1024 * 1024, max_events=128):
        if (not 1 <= ttl_seconds <= 1200 or not 1 <= max_bytes <= 48 * 1024 * 1024
                or not 1 <= max_events <= 256):
            raise ValueError("vision_cache_limits_invalid")
        self.clock, self.transport = clock, transport
        self.ttl_seconds, self.max_bytes, self.max_events = ttl_seconds, max_bytes, max_events
        self._entries = OrderedDict()
        self._pending = {}
        self._pending_meta = {}
        self._jobs = set()
        self._slots = asyncio.Semaphore(2)
        self._bytes = 0

    @staticmethod
    def _identity(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 512
                or any(ord(c) < 32 for c in value)):
            raise ValueError("vision_identity_invalid")
        return value

    @staticmethod
    def _public(row):
        return {key: value for key, value in row.items() if not key.startswith("_")}

    def _drop(self, key):
        entry = self._entries.pop(key, None)
        if entry:
            self._bytes -= entry["bytes"]

    def _prune(self):
        now = self.clock()
        for key, entry in list(self._entries.items()):
            if now - entry["at"] >= self.ttl_seconds:
                self._drop(key)
        while self._entries and (self._bytes > self.max_bytes or len(self._entries) > self.max_events):
            self._drop(next(iter(self._entries)))

    @staticmethod
    def _unavailable(scope, user, event, index, code):
        return {"image_id": uuid.uuid4().hex, "user_id": user, "event_id": event,
                "position": index, "status": "unavailable", "error_code": code}

    async def _download(self, url):
        validate_url(url)
        async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(12, connect=5),
                                     trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", url, headers={"Accept": "image/*"}) as response:
                if response.status_code != 200:
                    raise VisionError("vision_download_failed")
                length = response.headers.get("content-length", "")
                if length and (not length.isdecimal() or int(length) > MAX_DOWNLOAD_BYTES):
                    raise VisionError("vision_image_too_large")
                data = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        raise VisionError("vision_image_too_large")
                return await asyncio.to_thread(normalize_image, bytes(data))

    async def _prepare(self, key, urls):
        scope, user, event = key
        rows = []
        try:
            async with self._slots:
                for index, url in enumerate(urls):
                    if self._pending.get(key) is not asyncio.current_task():
                        return
                    row = self._unavailable(scope, user, event, index, "vision_download_failed")
                    try:
                        async with asyncio.timeout(15):
                            image = await self._download(url)
                    except VisionError as exc:
                        row["error_code"] = str(exc)
                    except Exception:
                        pass
                    else:
                        row.update({key: value for key, value in image.items() if key != "data"})
                        row.update(status="ready", error_code="", byte_size=len(image["data"]))
                        row["_data_url"] = "data:" + image["mime_type"] + ";base64," + base64.b64encode(image["data"]).decode("ascii")
                    rows.append(row)
            if self._pending.get(key) is asyncio.current_task():
                size = sum(len(row.get("_data_url", "")) for row in rows)
                # A tiny caller-specified cache still must not briefly expose
                # data beyond its cap; fail this event rather than evict itself.
                if size > self.max_bytes:
                    rows = [self._unavailable(scope, user, event, i, "vision_cache_full") for i in range(len(urls))]
                    size = 0
                self._drop(key)
                self._entries[key] = {"at": self.clock(), "rows": rows, "bytes": size}
                self._bytes += size
                self._prune()
        finally:
            if self._pending.get(key) is asyncio.current_task():
                self._pending.pop(key, None)
                self._pending_meta.pop(key, None)

    async def prepare(self, scope, user, event_id, urls):
        key = tuple(self._identity(v) for v in (scope, user, event_id))
        if (not isinstance(urls, list) or len(urls) > MAX_IMAGES
                or any(not isinstance(url, str) or len(url) > 8192 for url in urls)):
            raise ValueError("vision_images_invalid")
        if not urls:
            return []
        self._prune()
        if key not in self._entries:
            task = self._pending.get(key)
            if task is None:
                if len(self._jobs) >= 32:
                    rows = [self._unavailable(*key, i, "vision_busy") for i in range(len(urls))]
                    self._entries[key] = {"at": self.clock(), "rows": rows, "bytes": 0}
                    self._prune()
                    return [self._public(row) for row in rows]
                task = asyncio.create_task(self._prepare(key, list(urls)))
                self._pending[key] = task
                self._pending_meta[key] = [self._unavailable(*key, i, "vision_pending") for i in range(len(urls))]
                self._jobs.add(task)
                task.add_done_callback(self._jobs.discard)
            # A cancelled HTTP request cannot cancel a second caller's work.
            await asyncio.shield(task)
        entry = self._entries.get(key)
        if entry is None:
            return [self._unavailable(*key, i, "vision_context_removed") for i in range(len(urls))]
        return [self._public(row) for row in entry["rows"]]

    async def wait_pending(self, scope, event_ids, *, timeout=45):
        """Wait for existing timeline images without creating any new fetches.

        Waiting is bounded even when jobs are queued behind other downloads.
        A timeout or cancellation only stops this waiter; a second message may
        still need the same image. ``metadata`` describes unfinished inputs as
        unavailable/vision_pending so callers cannot pretend they saw them.
        """
        self._identity(scope)
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 45:
            raise ValueError("vision_wait_timeout_invalid")
        ids = set(event_ids)
        waiting = [asyncio.shield(task) for (room, _, event), task in self._pending.items()
                   if room == scope and event in ids]
        if not waiting:
            return
        try:
            done, _ = await asyncio.wait(waiting, timeout=timeout)
            for future in done:
                try:
                    future.result()
                except (Exception, asyncio.CancelledError):
                    # Download failures are represented as safe metadata, never
                    # surfaced here as an HTTP exception carrying a signed URL.
                    pass
        finally:
            for future in waiting:
                if not future.done():
                    future.cancel()  # These are shields, not the shared jobs.

    def metadata(self, scope, event_ids):
        self._identity(scope)
        ids = set(event_ids)
        self._prune()
        rows = [self._public(row) for (room, _, event), entry in self._entries.items()
                if room == scope and event in ids for row in entry["rows"]]
        rows.extend(self._public(row) for (room, _, event), pending in self._pending_meta.items()
                    if room == scope and event in ids for row in pending)
        return rows

    def select(self, scope, event_ids, max_images=4):
        self._identity(scope)
        if not isinstance(max_images, int) or isinstance(max_images, bool) or not 0 <= max_images <= 8:
            raise ValueError("vision_selection_limit_invalid")
        priorities = {event: index for index, event in enumerate(event_ids)}
        self._prune()
        selected = []
        entries = [(key, entry) for key, entry in self._entries.items()
                   if key[0] == scope and key[2] in priorities]
        for (room, _, event), entry in sorted(entries, key=lambda pair: priorities[pair[0][2]], reverse=True):
            for row in entry["rows"]:
                if row["status"] == "ready" and len(selected) < max_images:
                    selected.append(dict(self._public(row), data_url=row["_data_url"]))
        return selected

    def clear_scope(self, scope):
        self._identity(scope)
        for key in list(self._entries):
            if key[0] == scope:
                self._drop(key)
        for key in list(self._pending):
            if key[0] == scope:
                self._pending.pop(key, None)
                self._pending_meta.pop(key, None)

    def remove_user(self, scope, user):
        self._identity(scope); self._identity(user)
        for key in list(self._entries):
            if key[:2] == (scope, user):
                self._drop(key)
        for key in list(self._pending):
            if key[:2] == (scope, user):
                self._pending.pop(key, None)
                self._pending_meta.pop(key, None)

    def clear(self):
        self._entries.clear()
        self._pending.clear()
        self._pending_meta.clear()
        self._bytes = 0
