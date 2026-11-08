"""Readable, bounded notification cards; only synthetic platform data/images."""
import asyncio
import io
from pathlib import Path
import random

import httpx
from PIL import Image, ImageDraw
import pytest

from azusa_bot import subscription_cards as module
from azusa_bot.subscription_cards import Cards, MAX_CARD_BYTES, MAX_CARD_HEIGHT


def png(color, size=(480, 270)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def drawn(monkeypatch):
    values = []
    original = ImageDraw.ImageDraw.text

    def capture(self, position, text, *args, **kwargs):
        values.append((position, text))
        return original(self, position, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    return values


def info(blocks, **extra):
    return {"type": "bilibili_dynamic", "name": "合成测试作者", "published_at": 1700000000,
            "card": {"blocks": blocks}, **extra}


async def test_readable_body_multiple_images_and_forward_keep_order(tmp_path, drawn):
    urls = ["https://i0.hdslb.com/a.png", "https://i1.hdslb.com/b.png", "https://i2.hdslb.com/c.png"]
    calls = []
    def handler(request):
        calls.append(str(request.url))
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, content=png({urls[0]: "red", urls[1]: "blue", urls[2]: "green"}[str(request.url)]), headers={"content-type": "image/png"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        card = info([{"text": "\n".join("正文第%d行" % number for number in range(1, 13)), "images": urls[:2]},
                     {"label": "转发 @另一位作者", "title": "原文标题", "text": "保留原作者表达，不生成摘要", "images": urls[2:]}])
        path, error = await Cards(tmp_path, client).render(card, "旧摘要")
    assert not error and calls == urls
    with Image.open(path) as image:
        assert image.width == 960 and 1800 < image.height <= MAX_CARD_HEIGHT
        rows = [(y, image.convert("RGB").getpixel((480, y))) for y in range(0, image.height, 40)]
        first = {color: next(y for y, pixel in rows if pixel == color) for color in ((255, 0, 0), (0, 0, 255), (0, 128, 0))}
        assert first[(255, 0, 0)] < first[(0, 0, 255)] < first[(0, 128, 0)]
    contents = [text for _, text in drawn]
    assert "正文第12行" in contents and "旧摘要" not in contents
    assert "转发 @另一位作者" in contents and "原文标题" in contents
    body_y = next(position[1] for position, text in drawn if text == "正文第12行")
    forward_y = next(position[1] for position, text in drawn if text == "转发 @另一位作者")
    assert body_y < first[(255, 0, 0)] < first[(0, 0, 255)] < forward_y < first[(0, 128, 0)]
    assert any("北京时间" in text for text in contents)


async def test_failed_untrusted_and_redirect_images_leave_visible_content(tmp_path, drawn):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        path, error = await Cards(tmp_path, client).render(info([{"text": "即使图片失败也能读正文", "images": ["http://127.0.0.1/private", "https://i0.hdslb.com/redirect.png"]}]), "消息")
    assert Path(path).is_file() and error == "subscription_image_unavailable"
    assert calls == ["https://i0.hdslb.com/redirect.png"]
    assert any(text == "即使图片失败也能读正文" for _, text in drawn)
    assert sum("图片暂未加载" in text for _, text in drawn) == 2


async def test_batch_timeout_retains_loaded_photo_and_marks_missing(tmp_path, drawn, monkeypatch):
    cards = Cards(tmp_path)
    active, peak = 0, 0
    async def load(url):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        try:
            if url.endswith("/0"):
                return Image.new("RGB", (30, 30), "red")
            await asyncio.sleep(5)
        finally:
            active -= 1
    monkeypatch.setattr(cards, "_image", load)
    monkeypatch.setattr(module, "IMAGE_BATCH_TIMEOUT", .02)
    path, error = await asyncio.wait_for(cards.render(info([{"text": "内容", "images": [f"https://i0.hdslb.com/{n}" for n in range(9)]}]), "消息"), 2)
    assert Path(path).is_file() and error == "subscription_image_unavailable"
    assert peak <= 3 and active == 0
    assert any("图片暂未加载" in text for _, text in drawn)


async def test_limits_are_visible_and_card_cache_key_covers_rich_content(tmp_path, drawn, monkeypatch):
    cards, calls = Cards(tmp_path), []
    async def load(url):
        calls.append(url)
        return None
    monkeypatch.setattr(cards, "_image", load)
    card = info([{"title": "标题", "text": "长正文\n" * 2000, "images": [f"https://i0.hdslb.com/{n}" for n in range(20)], "images_total": 20},
                 {"text": "末尾转发", "truncated": True}])
    first, _ = await cards.render(card, "固定摘要")
    assert len(calls) == 10  # nine images plus optional avatar
    with Image.open(first) as image:
        assert image.width == 960 and image.height <= MAX_CARD_HEIGHT
    assert any("完整内容见随附链接" in text for _, text in drawn)
    card["card"]["blocks"][0]["title"] = "不同标题"
    second, _ = await cards.render(card, "固定摘要")
    assert first != second


async def test_more_images_and_platform_truncation_are_labeled(tmp_path, drawn, monkeypatch):
    cards = Cards(tmp_path)
    async def no_image(url):
        return None
    monkeypatch.setattr(cards, "_image", no_image)
    await cards.render(info([{"text": "平台已截断的长文", "truncated": True,
                             "images": [], "images_total": 12}]), "旧摘要")
    assert any("正文仅展示部分" in text for _, text in drawn)
    assert any("另有 12 张配图" in text for _, text in drawn)


async def test_long_photo_preserves_both_ends_and_reports_scaling(tmp_path, drawn, monkeypatch):
    cards = Cards(tmp_path)
    photo = Image.new("RGB", (400, 4000), "white")
    ImageDraw.Draw(photo).rectangle((0, 0, 399, 1000), fill="red")
    ImageDraw.Draw(photo).rectangle((0, 3000, 399, 3999), fill="blue")
    async def load(url):
        return photo if url else None
    monkeypatch.setattr(cards, "_image", load)
    path, error = await cards.render(info([{"text": "长截图", "images": ["https://i0.hdslb.com/long.png"]}]), "消息")
    with Image.open(path) as image:
        colors = {image.getpixel((480, y)) for y in range(200, image.height-100, 30)}
    assert not error and (255, 0, 0) in colors and (0, 0, 255) in colors
    assert any("长图已缩放" in text for _, text in drawn)


async def test_high_entropy_multiple_photos_fit_qq_png_limit(tmp_path, monkeypatch):
    cards = Cards(tmp_path)
    # A photographic/noise card exceeds 8 MiB as RGB even at the bounded height.
    photo = Image.frombytes("RGB", (812, 1000), random.Random(7).randbytes(812*1000*3))
    async def load(url):
        return photo if url else None
    monkeypatch.setattr(cards, "_image", load)
    path, error = await cards.render(info([{"text": "九张合成高熵图片", "images": [f"https://i0.hdslb.com/{n}" for n in range(9)]}]), "消息")
    assert not error and 0 < Path(path).stat().st_size <= MAX_CARD_BYTES < 8*1024*1024
    with Image.open(path) as image:
        assert image.width == 960 and 9000 < image.height <= MAX_CARD_HEIGHT
        assert image.mode == "P"  # byte cap retained dimensions via indexed PNG


async def test_legacy_post_and_steam_keep_supported_contract(tmp_path, drawn, monkeypatch):
    cards = Cards(tmp_path)
    async def load(url):
        return Image.new("RGB", (400, 300), "blue") if url else None
    monkeypatch.setattr(cards, "_image", load)
    for kind in ("weibo", "steam"):
        path, error = await cards.render({"type": kind, "name": "旧缓存", "cover": "https://i0.hdslb.com/photo"}, "保留旧事件正文\nhttps://example.test/post")
        assert not error
        with Image.open(path) as image:
            assert image.width == 960
            if kind == "steam":
                assert image.size == (960, 720)
    assert any(text == "保留旧事件正文" for _, text in drawn)


async def test_image_cache_preserves_readable_long_shape_and_has_pixel_bound(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            content=png("red", (1600, 6000)), headers={"content-type": "image/png"}))) as client:
        cards = Cards(tmp_path, client)
        image = await cards._image("https://i0.hdslb.com/long.png")
        assert image.height > 1280 and image.width*image.height <= 2_000_000
        cached = await cards._image("https://i0.hdslb.com/long.png")
        assert cached.size == image.size


@pytest.mark.parametrize("text,width,limit,lines,truncated", [
    ("a\n", 20, 1, ["a"], False),
    ("a\nb\n", 20, 2, ["a", "b"], False),
    ("\n\n", 20, 2, ["", ""], False),
    ("a\nb\nc", 20, 2, ["a", "b"], True),
    ("a\n\n", 20, 1, ["a"], True),
    ("ab", 1, 1, ["a"], True),
    ("a", 1, 1, ["a"], False),
])
def test_line_limit_distinguishes_final_newline_from_unseen_content(text, width, limit, lines, truncated):
    class Measure:
        def textlength(self, value, *, font):
            return len(value)
    assert module._post_lines(Measure(), text, None, width, limit) == (lines, truncated)
