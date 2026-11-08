"""Profile-local, reviewed expression images. No remote image fetches or paths from models."""
from __future__ import annotations
from .host import safe_open, NOFOLLOW

import base64
import binascii
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid
import warnings

from PIL import Image, ImageOps

from .settings import private_write

MAX_BYTES = 3 * 1024 * 1024
MAX_FILES = 100
MAX_LIBRARY_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_FRAMES = 120
MAX_TOTAL_PIXELS = 48_000_000
LIMITS = dict(max_bytes=MAX_BYTES, max_files=MAX_FILES, max_library_bytes=MAX_LIBRARY_BYTES,
              max_pixels=MAX_PIXELS, max_frames=MAX_FRAMES, max_total_pixels=MAX_TOTAL_PIXELS)
_LOCKS, _LOCK_GUARD = {}, threading.Lock()
_META_CACHE = {}
_ID = re.compile(r"(?:[a-f0-9]{32}|legacy-[a-f0-9]{24})\Z")
_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class AssetError(ValueError):
    pass


def _label(value, maximum, *, required=False):
    if (not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value)
            or (required and not value.strip())):
        raise AssetError("asset_label_invalid")
    return value.strip()


def _fields(body, old=None):
    old = old or {}
    if not isinstance(body, dict) or set(body)-{"name", "keywords", "emotion", "approved", "auto_send"}:
        raise AssetError("asset_fields_invalid")
    name = _label(body.get("name", old.get("name", "")), 40, required=True)
    if any(c in name for c in "/\\") or name in {".", ".."}:
        raise AssetError("asset_name_invalid")
    keywords = body.get("keywords", old.get("keywords", []))
    if not isinstance(keywords, list) or len(keywords) > 12:
        raise AssetError("asset_keywords_invalid")
    keywords = list(dict.fromkeys(_label(item, 24, required=True) for item in keywords))
    emotion = _label(body.get("emotion", old.get("emotion", "")), 24)
    approved = body.get("approved", old.get("approved", True))
    auto_send = body.get("auto_send", old.get("auto_send", False))
    if type(approved) is not bool or type(auto_send) is not bool:
        raise AssetError("asset_flags_invalid")
    return dict(name=name, keywords=keywords, emotion=emotion, approved=approved, auto_send=auto_send)


def _image(raw, *, normalize=False):
    if not raw or len(raw) > MAX_BYTES:
        raise AssetError("asset_size_exceeded")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                fmt = source.format
                if fmt not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise AssetError("asset_format_invalid")
                count = getattr(source, "n_frames", 1)
                pixels = source.width * source.height
                if (pixels > MAX_PIXELS or count > MAX_FRAMES or pixels * count > MAX_TOTAL_PIXELS
                        or source.width < 1 or source.height < 1):
                    raise AssetError("asset_dimensions_exceeded")
                if count > 1 and fmt != "GIF":
                    raise AssetError("asset_animation_requires_gif")
                frames, durations = [], []
                for i in range(count):
                    source.seek(i)
                    source.load()
                    if normalize:
                        frame = ImageOps.exif_transpose(source).convert("RGBA" if fmt != "JPEG" else "RGB")
                        # Start a clean image so source EXIF/comment metadata is not copied.
                        clean = Image.new(frame.mode, frame.size)
                        clean.paste(frame)
                        frames.append(clean)
                        durations.append(max(20, min(10000, int(source.info.get("duration", 100)))))
                width, height = source.size
                if normalize:
                    out = io.BytesIO()
                    if fmt == "GIF":
                        frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                                       duration=durations, loop=max(0, min(65535, int(source.info.get("loop", 0)))),
                                       disposal=2)
                    else:
                        frames[0].save(out, format=fmt, **({"quality":92} if fmt == "JPEG" else {}))
                    raw = out.getvalue()
                    width, height = frames[0].size
                    if len(raw) > MAX_BYTES:
                        raise AssetError("asset_size_exceeded")
                return raw, dict(format=fmt.lower(), width=width, height=height, frames=count, bytes=len(raw))
    except AssetError:
        raise
    except (OSError, ValueError, TypeError, EOFError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AssetError("asset_image_invalid") from None


class AssetStore:
    def __init__(self, content_root: Path):
        self.root = Path(content_root).resolve()
        self.base = self.root / "content/assets"
        with _LOCK_GUARD:
            self.lock = _LOCKS.setdefault(str(self.base), threading.RLock())

    def _ensure(self):
        if (self.root / "content").is_symlink() or self.base.is_symlink():
            raise AssetError("asset_directory_invalid")
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _id(name, item):
        number = item.get("id") or "legacy-" + hashlib.sha256((name+"\0"+str(item.get("file", ""))).encode()).hexdigest()[:24]
        if not isinstance(number, str) or not _ID.fullmatch(number):
            raise AssetError("asset_manifest_invalid")
        return number

    def _read(self):
        self._ensure()
        path = self.base / "manifest.json"
        if not path.exists() and not path.is_symlink():
            return {}
        if path.is_symlink() or path.stat().st_size > 512 * 1024:
            raise AssetError("asset_manifest_invalid")
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or len(data) > MAX_FILES:
                raise ValueError()
            seen = set()
            for name, item in data.items():
                if not isinstance(name, str) or not isinstance(item, dict):
                    raise ValueError()
                number = self._id(name, item)
                if number in seen:
                    raise ValueError()
                seen.add(number)
            return data
        except (OSError, ValueError, TypeError):
            raise AssetError("asset_manifest_invalid") from None

    def _path(self, item):
        value = item.get("file")
        if not isinstance(value, str) or len(value) > 200:
            raise AssetError("asset_file_invalid")
        path = Path(value)
        if path.is_absolute() or not path.parts or any(p in {"..", "."} for p in path.parts) or path.suffix.lower() not in _EXTENSIONS:
            raise AssetError("asset_file_invalid")
        current = self.base
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise AssetError("asset_file_invalid")
        if not current.is_file() or current.stat().st_size > MAX_BYTES:
            raise AssetError("asset_file_invalid")
        return current

    def _bytes(self, item):
        path = self._path(item)
        descriptor = safe_open(path, os.O_RDONLY | NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise AssetError("asset_file_invalid")
            raw = stream.read(MAX_BYTES+1)
        raw, metadata = _image(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if item.get("sha256") and item["sha256"] != digest:
            raise AssetError("asset_file_changed")
        return raw, metadata, digest

    def _public(self, name, item):
        keywords = item.get("keywords", [])
        keywords = [value[:24] for value in keywords if isinstance(value, str)][:12] if isinstance(keywords, list) else []
        emotion = item.get("emotion", "")
        record = dict(id=self._id(name, item), name=name, keywords=keywords,
                      emotion=emotion[:24] if isinstance(emotion, str) else "", approved=item.get("approved") is True,
                      auto_send=item.get("auto_send") is True, available=False)
        try:
            # Avoid decoding every full animation on every admin list / context retrieval.
            path = self._path(item)
            state = path.stat()
            key = (str(path), state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)
            with _LOCK_GUARD:
                cached = _META_CACHE.get(key)
            if cached:
                metadata, digest = cached
            else:
                _, metadata, digest = self._bytes(item)
                with _LOCK_GUARD:
                    if len(_META_CACHE) >= 256:
                        _META_CACHE.pop(next(iter(_META_CACHE)))
                    _META_CACHE[key] = metadata, digest
            if item.get("sha256") and item["sha256"] != digest:
                raise AssetError("asset_file_changed")
            record.update(metadata, available=True)
        except (AssetError, OSError):
            record.update(format="", width=0, height=0, frames=0, bytes=0)
        return record

    def list(self):
        with self.lock:
            return {"assets": [self._public(name, item) for name, item in self._read().items()], "limits": LIMITS}

    def _find(self, data, number):
        found = [(name, item) for name, item in data.items() if self._id(name, item) == number]
        if len(found) != 1:
            raise AssetError("asset_not_found")
        return found[0]

    def upload(self, body):
        if not isinstance(body, dict) or set(body)-{"name", "content_base64", "keywords", "emotion", "approved", "auto_send"}:
            raise AssetError("asset_fields_invalid")
        item = _fields({k:v for k,v in body.items() if k != "content_base64"})
        encoded = body.get("content_base64")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES+2)//3):
            raise AssetError("asset_size_exceeded")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AssetError("asset_image_invalid") from None
        raw, metadata = _image(raw, normalize=True)
        from .lore import backup_guard
        with self.lock, backup_guard(self.root):
            data = self._read()
            if item["name"] in data:
                raise AssetError("asset_name_exists")
            if len(data) >= MAX_FILES:
                raise AssetError("asset_library_limit")
            total = 0
            for value in data.values():
                try:
                    total += self._path(value).stat().st_size
                except (AssetError, OSError):
                    pass  # Unavailable legacy entries remain editable/removable.
            if total+len(raw) > MAX_LIBRARY_BYTES:
                raise AssetError("asset_library_limit")
            number = uuid.uuid4().hex
            extension = ".jpg" if metadata["format"] == "jpeg" else "."+metadata["format"]
            item.update(id=number, file=number+extension, sha256=hashlib.sha256(raw).hexdigest(),
                        revision=uuid.uuid4().hex, **metadata)
            path = self.base / item["file"]
            descriptor = safe_open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            data[item["name"]] = {k:v for k,v in item.items() if k != "name"}
            try:
                private_write(self.base / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return self._public(item["name"], data[item["name"]])

    def update(self, number, body):
        from .lore import backup_guard
        with self.lock, backup_guard(self.root):
            data = self._read()
            name, old = self._find(data, number)
            fields = _fields(body, dict(old, name=name))
            if fields["name"] != name and fields["name"] in data:
                raise AssetError("asset_name_exists")
            data.pop(name)
            item = dict(old, **{k:v for k,v in fields.items() if k != "name"}, id=number, revision=uuid.uuid4().hex)
            data[fields["name"]] = item
            private_write(self.base / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
            return self._public(fields["name"], item)

    def delete(self, number):
        from .lore import backup_guard
        with self.lock, backup_guard(self.root):
            data = self._read()
            name, item = self._find(data, number)
            # Remove eligibility first, then remove only an owned managed blob.
            data.pop(name)
            private_write(self.base / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
            try:
                path = self._path(item)
                owned = (re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|gif|webp)", path.name)
                         and item.get("id") == path.stem and bool(item.get("sha256")))
                if owned and not any(v.get("file") == item["file"] for v in data.values()):
                    path.unlink()
            except (AssetError, OSError):
                pass
            return {"deleted": True}

    def preview(self, number):
        with self.lock:
            _, item = self._find(self._read(), number)
            raw, metadata, _ = self._bytes(item)
            return raw, "image/"+metadata["format"]

    def catalog(self, query="", limit=8):
        from .lore import terms
        query_terms = terms(query)
        rows = []
        with self.lock:
            for name, item in self._read().items():
                if item.get("approved") is not True or item.get("auto_send") is not True:
                    continue
                keywords = item.get("keywords", [])
                keywords = [value for value in keywords if isinstance(value, str)] if isinstance(keywords, list) else []
                words = terms(" ".join([name, str(item.get("emotion", "")), *keywords]))
                score = len(query_terms & words)
                if query_terms and not score:
                    continue
                rows.append((score, name, item))
            rows.sort(key=lambda value:(-value[0], self._id(value[1], value[2])))
            selected = []
            for _, name, item in rows:
                if len(selected) >= max(0, min(limit, 20)):
                    break
                row = self._public(name, item)
                if row["available"]:
                    selected.append(dict({key:row[key] for key in ("id", "name", "keywords", "emotion")},
                                         revision=item.get("revision", "legacy")))
            return selected

    def prepare(self, media_root: Path, selector: str, *, automatic=False):
        if not isinstance(selector, str) or type(automatic) is not bool:
            raise AssetError("asset_selector_invalid")
        with self.lock:
            data = self._read()
            try:
                name, item = self._find(data, selector)
            except AssetError:
                if selector not in data:
                    raise AssetError("asset_not_found") from None
                name, item = selector, data[selector]
            if item.get("approved") is not True or automatic and item.get("auto_send") is not True:
                raise AssetError("asset_not_approved")
            raw, metadata, digest = self._bytes(item)
            destination = Path(media_root)
            if destination.is_symlink():
                raise AssetError("asset_directory_invalid")
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            extension = ".jpg" if metadata["format"] == "jpeg" else "."+metadata["format"]
            target = destination / (uuid.uuid4().hex+extension)
            descriptor = safe_open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
            return {"mode":"image", "text":"", "media_path":str(target.resolve()),
                    "asset_reference":{"id":self._id(name,item), "revision":item.get("revision", "legacy"),
                                       "sha256":digest, "automatic":automatic}}

    def allowed(self, reference):
        if (not isinstance(reference, dict) or set(reference) != {"id", "revision", "sha256", "automatic"}
                or type(reference.get("automatic")) is not bool):
            return False
        with self.lock:
            try:
                _, item = self._find(self._read(), reference["id"])
                if (item.get("approved") is not True or reference["automatic"] and item.get("auto_send") is not True
                        or reference["revision"] != item.get("revision", "legacy")):
                    return False
                return self._bytes(item)[2] == reference["sha256"]
            except (AssetError, OSError):
                return False


def catalog(content_root: Path, query="", limit=8):
    try:
        return AssetStore(content_root).catalog(query, limit)
    except (AssetError, OSError):
        # Expressions are optional. A broken manually maintained manifest must
        # not disable ordinary chat; the admin list still reports the error.
        return []


def prepare_asset(content_root: Path, media_root: Path, selector: str, *, automatic=False):
    try:
        return AssetStore(content_root).prepare(media_root, selector, automatic=automatic)
    except OSError:
        raise AssetError("asset_file_unavailable") from None


def still_approved(content_root: Path, reference):
    return AssetStore(content_root).allowed(reference)


def resolve_references(content_root: Path, references):
    """Resolve historical candidate IDs without reviving stale names or paths."""
    if not isinstance(references, (list, tuple)):
        return []
    result = []
    for ref in references[:20]:
        if not isinstance(ref, dict):
            continue
        number, revision = ref.get("id"), ref.get("revision")
        if (not isinstance(number, str) or not _ID.fullmatch(number)
                or not isinstance(revision, str)
                or (revision != "legacy" and not re.fullmatch(r"[a-f0-9]{32}", revision))):
            continue
        result.append({"id": number, "revision": revision, "available": False})
    try:
        store = AssetStore(content_root)
        with store.lock:
            data = store._read()
            by_id = {store._id(name, item): (name, item) for name, item in data.items()}
            for row in result:
                match = by_id.get(row["id"])
                if match is None:
                    continue
                name, item = match
                if (item.get("revision", "legacy") != row["revision"]
                        or item.get("approved") is not True or item.get("auto_send") is not True):
                    continue
                if store._public(name, item)["available"]:
                    row.update(name=name, available=True)
    except (AssetError, OSError):
        # Corrupt or deleted optional asset stores must not break the trace UI.
        return [{key: row[key] for key in ("id", "revision")} | {"available": False} for row in result]
    return result


