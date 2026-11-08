"""Small local Pillow cards with bounded, credential-free CDN image reads."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
import warnings
import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_POST_IMAGES = 9
MAX_CARD_HEIGHT = 10_000
MAX_CARD_BYTES = 15 * 1024 * 1024 // 2  # Below the bridge's 8 MiB PNG limit.
IMAGE_BATCH_TIMEOUT = 20
_POST_TYPES = {"bilibili_dynamic", "bilibili_video", "weibo"}


def safe_image_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        allowed = (re.fullmatch(r"i[0-2]\.hdslb\.com", host) is not None
            or re.fullmatch(r"(?:wx|tvax|tva)[1-4]\.sinaimg\.cn", host) is not None or host in {
            "avatars.steamstatic.com", "avatars.akamai.steamstatic.com", "avatars.fastly.steamstatic.com",
            "shared.akamai.steamstatic.com", "shared.fastly.steamstatic.com",
            "cdn.akamai.steamstatic.com", "cdn.cloudflare.steamstatic.com"})
        if (not allowed or url.scheme not in {"https", "http"} or url.port is not None
                or url.username or url.password or not url.path.startswith("/")):
            return ""
        return urlunsplit(("https", host, url.path, url.query, ""))
    except ValueError:
        return ""


def _decode(raw):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_PIXELS or source.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise ValueError()
            source.seek(0)
            return ImageOps.exif_transpose(source).convert("RGB")


def _write_image(path, image, *, max_bytes=None):
    # A cancelled download may finish writing in its worker thread. Unique
    # temporary names keep a later preview from racing that atomic write.
    descriptor, temporary = tempfile.mkstemp(prefix=".card-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            image.save(stream, format="PNG", optimize=True)
            if max_bytes is not None and stream.tell() > max_bytes:
                # Indexed PNG keeps the original layout and text dimensions.
                # This is only used for large photographic notification cards.
                reduced = image.quantize(colors=128, method=Image.Quantize.FASTOCTREE)
                for attempt in range(6):
                    stream.seek(0); stream.truncate()
                    reduced.save(stream, format="PNG", optimize=True)
                    if stream.tell() <= max_bytes:
                        break
                    reduced = reduced.resize((int(reduced.width*.9), int(reduced.height*.9)))
                else:
                    raise ValueError("subscription_card_too_large")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _font(size):
    for path in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _wrap(draw, text, font, width, limit):
    lines, line = [], ""
    for char in str(text):
        if char == "\n" or draw.textlength(line + char, font=font) > width:
            lines.append(line)
            line = "" if char == "\n" else char
            if len(lines) == limit:
                lines[-1] = lines[-1][:-1] + "…"
                return lines
        else:
            line += char
    if line:
        lines.append(line)
    return lines[:limit]


def _post_blocks(info, text):
    """Defensively bound the rendering contract, including old cached events."""
    card = info.get("card")
    values = card.get("blocks") if isinstance(card, dict) else None
    if not isinstance(values, list) or not values:
        values = [{"text": text.rsplit("\nhttps://", 1)[0],
                   "images": [info["cover"]] if info.get("cover") else []}]
    blocks, remaining = [], MAX_POST_IMAGES
    for raw in values[:2]:
        if not isinstance(raw, dict):
            continue
        body = raw.get("text") if isinstance(raw.get("text"), str) else ""
        title = raw.get("title") if isinstance(raw.get("title"), str) else ""
        images = raw.get("images") if isinstance(raw.get("images"), list) else []
        total = raw.get("images_total")
        total = max(len(images), total if type(total) is int and total >= 0 else 0)
        selected = images[:remaining]
        remaining -= len(selected)
        blocks.append({"label": str(raw.get("label") or "")[:160], "title": title[:500],
            "text": body[:5000], "images": [url if isinstance(url, str) and len(url) <= 2048 else "" for url in selected],
            "images_total": min(total, 10000),
            "truncated": bool(raw.get("truncated")) or len(body) > 5000 or len(title) > 500})
    return blocks or [{"label": "", "title": "", "text": "原始内容见随附链接", "images": [], "images_total": 0, "truncated": True}]


def _post_lines(draw, text, font, width, limit):
    """Return visible lines and explicit overflow; preserve paragraph breaks."""
    lines, line = [], ""
    for index, char in enumerate(text):
        if char == "\n" or draw.textlength(line + char, font=font) > width:
            lines.append(line)
            line = "" if char == "\n" else char
            if len(lines) >= limit:
                # A final newline closes the last visible line; it does not
                # mean content was omitted. Width wrapping retains a character.
                return lines, bool(line) or index < len(text) - 1
        elif char not in "\r\x00":
            line += char
    if line:
        lines.append(line)
    return lines, False


class Cards:
    def __init__(self, root, client=None):
        self.media = Path(root) / "data/media"
        self.cache = Path(root) / "data/cache/subscription-images"
        self.client = client
        self._lock = asyncio.Lock()
        for path in (self.media, self.cache):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def _image(self, value):
        url = safe_image_url(value)
        if not url:
            return None
        path = self.cache / (hashlib.sha256(("image-v2:" + url).encode()).hexdigest() + ".png")
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 8 * 1024 * 1024:
            try:
                return await asyncio.to_thread(_decode, path.read_bytes())
            except Exception:
                path.unlink(missing_ok=True)
        async def fetch(client):
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                    raise ValueError()
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > MAX_IMAGE_BYTES:
                        raise ValueError()
            picture = await asyncio.to_thread(_decode, raw)
            picture.thumbnail((1280, 3200))
            if picture.width * picture.height > 2_000_000:
                ratio = (2_000_000 / (picture.width * picture.height)) ** .5
                picture.thumbnail((int(picture.width * ratio), int(picture.height * ratio)))
            await asyncio.to_thread(_write_image, path, picture)
            return picture
        if self.client is not None:
            return await fetch(self.client)
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4), trust_env=False, follow_redirects=False) as client:
            return await fetch(client)

    def _render(self, info, text, photo, avatar, path):
        image = Image.new("RGB", (960, 720 if photo is not None else 440), "#f4f2ee")
        draw = ImageDraw.Draw(image)
        accent = "#457a61" if info["type"] == "steam" else "#a54861"
        draw.rounded_rectangle((26, 26, 934, image.height - 26), radius=24, fill="#ffffff")
        x = 58
        if avatar is not None:
            avatar = ImageOps.fit(avatar, (64, 64))
            mask = Image.new("L", (64, 64)); ImageDraw.Draw(mask).ellipse((0, 0, 63, 63), fill=255)
            image.paste(avatar, (58, 50), mask); x = 140
        name=_wrap(draw,info.get("name", "订阅通知"),_font(29),900-x,1)
        draw.text((x, 48), name[0] if name else "订阅通知", font=_font(29), fill="#252b31")
        labels = {"bilibili_live": "BILIBILI · 开播", "bilibili_dynamic": "BILIBILI · 动态", "bilibili_video": "BILIBILI · 投稿", "steam": "STEAM · 好友状态", "weibo": "WEIBO · 微博"}
        draw.text((x, 89), labels[info["type"]], font=_font(17), fill=accent)
        y = 140
        if photo is not None:
            if info["type"] in {"bilibili_dynamic", "bilibili_video", "weibo"}:
                photo=ImageOps.contain(photo,(844,280),method=Image.Resampling.LANCZOS)
                image.paste(photo,(58+(844-photo.width)//2,y+(280-photo.height)//2))
            else:
                photo=ImageOps.fit(photo,(844,280),method=Image.Resampling.LANCZOS)
                image.paste(photo,(58,y))
            y += 304
        content = text.rsplit("\nhttps://", 1)[0]
        for line in _wrap(draw, content, _font(27), 844, 5):
            draw.text((58, y), line, font=_font(27), fill="#29313b"); y += 41
        draw.text((58, image.height - 64), "原始内容见随附链接 · 私人订阅", font=_font(17), fill="#667078")
        _write_image(path, image, max_bytes=MAX_CARD_BYTES)

    def _render_post(self, info, blocks, pictures, avatar, path):
        """Measure before allocating; a notification remains one bounded PNG."""
        fonts = {size: _font(size) for size in (17, 20, 27, 29, 32)}
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        operations, panels = [], []
        y, end, index, omitted = 148, MAX_CARD_HEIGHT - 126, 0, False

        def paragraph(text, size=27, color="#29313b", limit=180):
            nonlocal y
            if not text:
                return False
            available = max(0, (end-y)//(size+14))
            if not available:
                return True
            lines, cut = _post_lines(measure, text, fonts[size], 812, min(limit, available))
            for line in lines:
                operations.append(("text", 74, y, line, size, color)); y += size+14
            return cut

        for position, block in enumerate(blocks):
            if y + 100 > end:
                omitted = True
                break
            panel_start = y - 12
            if position or block["label"]:
                y += 12
                omitted |= paragraph(block["label"] or "转发原文", 20, "#6c5c70", 3)
                y += 10
            if block["title"]:
                omitted |= paragraph(block["title"], 32, "#252b31", 12)
                y += 12
            omitted |= paragraph(block["text"])
            if block["text"]:
                y += 18
            if block["truncated"]:
                omitted |= paragraph("正文仅展示部分 · 完整内容见原帖", 20, "#786274", 2)
                y += 12
            for number, _ in enumerate(block["images"]):
                photo = pictures[index]; index += 1
                if y + 150 > end:
                    omitted = True
                    continue
                count = block["images_total"] or len(block["images"])
                label = f"配图 {number + 1} / {count}"
                if photo is None:
                    operations.append(("missing", 74, y, 812, 96, label + " · 图片暂未加载，请打开原帖"))
                    y += 116
                    continue
                height = min(2200, int(812 * photo.height / photo.width), end-y-56)
                height = max(1, height)
                width = min(812, int(height * photo.width / photo.height))
                scaled = height < int(812 * photo.height / photo.width)
                if scaled:
                    label += " · 长图已缩放，原尺寸见链接"
                operations.append(("photo", 74+(812-width)//2, y, max(1, width), height, photo))
                y += height+10
                omitted |= paragraph(label, 17, "#778087", 2)
                y += 18
            if block["images_total"] > len(block["images"]):
                omitted |= paragraph(f"另有 {block['images_total']-len(block['images'])} 张配图 · 请打开原帖查看", 20, "#786274", 2)
                y += 12
            if position:
                panels.append((54, panel_start, 906, min(y, end)))
            y += 24

        height = min(MAX_CARD_HEIGHT, max(340, y + 104))
        image = Image.new("RGB", (960, height), "#f4f2ee")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((26, 26, 934, height-26), radius=24, fill="#ffffff")
        for bounds in panels:
            draw.rounded_rectangle(bounds, radius=14, fill="#f5f2f6")
        x = 74
        if avatar is not None:
            rounded = ImageOps.fit(avatar, (64, 64))
            mask = Image.new("L", (64, 64)); ImageDraw.Draw(mask).ellipse((0, 0, 63, 63), fill=255)
            image.paste(rounded, (74, 50), mask); x = 156
        name = _wrap(draw, info.get("name", "订阅通知"), fonts[29], 886-x, 1)
        draw.text((x, 48), name[0] if name else "订阅通知", font=fonts[29], fill="#252b31")
        label = "BILIBILI · 投稿" if info["type"] == "bilibili_video" else "BILIBILI · 动态" if info["type"] == "bilibili_dynamic" else "WEIBO · 微博"
        published = info.get("published_at")
        if type(published) in {int, float} and 0 < published < 253402214400:
            label += "  ·  " + datetime.fromtimestamp(published, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") + " 北京时间"
        draw.text((x, 91), label, font=fonts[17], fill="#a54861")
        for operation in operations:
            kind, x, top, *args = operation
            if kind == "text":
                value, size, color = args
                draw.text((x, top), value, font=fonts[size], fill=color)
            elif kind == "photo":
                width, photo_height, photo = args
                image.paste(photo.resize((width, photo_height), Image.Resampling.LANCZOS), (x, top))
            else:
                width, photo_height, label = args
                draw.rounded_rectangle((x, top, x+width, top+photo_height), radius=12, fill="#ecebef")
                draw.text((x+18, top+32), label, font=fonts[20], fill="#786274")
        footer = "内容较长，部分未展示 · 完整内容见随附链接" if omitted else "原文与原尺寸图片见随附链接 · 私人订阅"
        draw.line((74, height-88, 886, height-88), fill="#e8e4e7", width=1)
        draw.text((74, height-66), footer, font=fonts[17], fill="#667078")
        _write_image(path, image, max_bytes=MAX_CARD_BYTES)

    async def _pictures(self, urls):
        pictures = [None] * len(urls)
        semaphore = asyncio.Semaphore(3)
        failed = False

        async def load(index, url):
            nonlocal failed
            async with semaphore:
                try:
                    pictures[index] = await self._image(url)
                    failed |= bool(url) and pictures[index] is None
                except Exception:
                    failed = True

        try:
            async with asyncio.timeout(IMAGE_BATCH_TIMEOUT):
                await asyncio.gather(*(load(index, url) for index, url in enumerate(urls)))
        except TimeoutError:
            failed = True
        return pictures, "subscription_image_unavailable" if failed else ""

    async def render(self, info, text):
        # Concurrent local previews can share an identical card/cache destination.
        async with self._lock:
            post = info["type"] in _POST_TYPES
            blocks = _post_blocks(info, text) if post else []
            key = hashlib.sha256(json.dumps([info["type"], info.get("name"), text,
                info.get("cover"), info.get("avatar"), info.get("published_at"), blocks, "card-v2"], ensure_ascii=False).encode()).hexdigest()[:32]
            path = self.media / (key + ".png")
            urls = [url for block in blocks for url in block["images"]] if post else [info.get("cover", "")]
            urls.append(info.get("avatar", ""))
            pictures, error = await self._pictures(urls)
            if post and any(picture is None for picture in pictures[:-1]):
                error = "subscription_image_unavailable"
            try:
                if post:
                    await asyncio.to_thread(self._render_post, info, blocks, pictures[:-1], pictures[-1], path)
                else:
                    await asyncio.to_thread(self._render, info, text, *pictures, path)
                self.prune()
                return str(path.resolve()), error
            except Exception:
                return "", "subscription_card_unavailable"

    def prune(self):
        # Only this cache is size-pruned; never remove active outbox media files.
        files = sorted((p for p in self.cache.glob("*.png") if p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for path in files:
            total += path.stat().st_size
            if total > MAX_CACHE_BYTES or time.time() - path.stat().st_mtime > 7 * 86400:
                path.unlink(missing_ok=True)
