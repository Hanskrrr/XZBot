"""Synthetic pixels and MockTransport only; no QQ or cloud traffic."""
import asyncio
import base64
import io
import json

import httpx
from PIL import Image, PngImagePlugin
import pytest

from azusa_bot import vision
from azusa_bot.vision import VisionCache, VisionError, normalize_image, validate_url


URL = "https://multimedia.nt.qq.com.cn/download?appid=sample&rkey=private-synthetic-token"


def pixels(size=(24, 16), color="red", format="PNG", **kwargs):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format=format, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize("url", [
    "http://gchat.qpic.cn/a", "file:///etc/passwd", "data:image/png;base64,YQ==",
    "https://127.0.0.1/a", "https://[::1]/a", "https://169.254.169.254/a",
    "https://gchat.qpic.cn.evil.invalid/a", "https://evil.invalid@gchat.qpic.cn/a",
    "https://gchat.qpic.cn:444/a", "https://gchat.qpic.cn:443/a", "https://gchat.qpic.cn/a#secret",
    "https://gchat.qpic.cn\\@evil.invalid/a", "https://gchat.qpic.cn/a\n", "https://qpic.cn/a",
])
def test_url_allowlist_rejects_fetch_and_credential_abuse(url):
    with pytest.raises(VisionError, match="^vision_url_rejected$"):
        validate_url(url)


def test_normalization_strips_metadata_resizes_and_bounds_output():
    info = PngImagePlugin.PngInfo()
    info.add_text("private", "do not forward metadata")
    result = normalize_image(pixels((2000, 1800), pnginfo=info))
    assert result["width"] == 1536 and result["height"] <= 1536
    assert result["mime_type"] == "image/png"
    assert len(result["data"]) <= vision.MAX_IMAGE_BYTES
    assert b"do not forward metadata" not in result["data"]
    with Image.open(io.BytesIO(result["data"])) as image:
        assert image.info == {}
        assert image.mode == "RGB"


def test_gif_first_frame_is_explicit():
    out = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(out, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (20, 20), "blue")], duration=100, loop=0)
    result = normalize_image(out.getvalue())
    assert result["animated_first_frame"] is True
    with Image.open(io.BytesIO(result["data"])) as image:
        r, g, b = image.convert("RGB").getpixel((10, 10))
        assert r > 240 and b < 10


def test_rejects_invalid_svg_and_large_dimensions(monkeypatch):
    for data in [b"not an image", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>']:
        with pytest.raises(VisionError, match="vision_image_invalid"):
            normalize_image(data)
    with pytest.raises(VisionError, match="vision_format_unsupported"):
        normalize_image(pixels(format="BMP"))
    monkeypatch.setattr(vision, "MAX_PIXELS", 100)
    with pytest.raises(VisionError, match="vision_dimensions_exceeded"):
        normalize_image(pixels((20, 20)))


@pytest.mark.asyncio
async def test_prepare_metadata_is_clean_and_select_is_scope_bound():
    calls = []
    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    rows = await cache.prepare("group:one", "100", "-1", [URL])
    assert rows[0]["status"] == "ready"
    assert rows[0]["user_id"] == "100" and rows[0]["event_id"] == "-1"
    assert "url" not in json.dumps(rows).lower() and "base64" not in json.dumps(rows)
    assert "private-synthetic" not in json.dumps(rows)
    assert cache.metadata("group:one", ["-1"]) == rows
    assert cache.metadata("group:two", ["-1"]) == []
    assert cache.select("group:two", ["-1"]) == []
    selected = cache.select("group:one", ["-1"])
    assert len(selected) == 1
    assert base64.b64decode(selected[0]["data_url"].split(",", 1)[1]).startswith(b"\x89PNG")
    assert await cache.prepare("group:one", "100", "-1", [URL]) == rows
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_latest_event_priority_follows_caller_timeline_not_download_order():
    cache = VisionCache(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=pixels())))
    for event in ["current", "older"]:
        await cache.prepare("g", "u", event, [URL + str(i) for i in range(3)])
    rows = cache.select("g", ["older", "current"], max_images=4)
    assert [r["event_id"] for r in rows] == ["current", "current", "current", "older"]


@pytest.mark.asyncio
async def test_failures_are_explicit_and_never_leak_signed_url_or_follow_redirect():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    cache = VisionCache(transport=httpx.MockTransport(handler))
    rows = await cache.prepare("g", "u", "e", [URL, "https://127.0.0.1/private"])
    assert [r["error_code"] for r in rows] == ["vision_download_failed", "vision_url_rejected"]
    assert len(calls) == 1 and cache.select("g", ["e"]) == []
    assert "private" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_download_bound_applies_to_declared_and_streamed_body(monkeypatch):
    monkeypatch.setattr(vision, "MAX_DOWNLOAD_BYTES", 64)
    for headers in [{"content-length": "1000"}, {"content-length": "1"}]:
        cache = VisionCache(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers=headers, content=b"a" * 100)))
        rows = await cache.prepare("g", "u", "e", [URL])
        assert rows[0]["error_code"] == "vision_image_too_large"


@pytest.mark.asyncio
async def test_backend_exception_contains_no_url_in_metadata():
    def handler(request):
        raise RuntimeError(str(request.url))
    cache = VisionCache(transport=httpx.MockTransport(handler))
    rows = await cache.prepare("g", "u", "e", [URL])
    assert rows[0]["error_code"] == "vision_download_failed"
    assert "private-synthetic" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_concurrent_same_event_downloads_once_and_erasure_cannot_resurrect():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    first = asyncio.create_task(cache.prepare("g", "u", "e", [URL]))
    await entered.wait()
    second = asyncio.create_task(cache.prepare("g", "u", "e", [URL]))
    await asyncio.sleep(0)
    cache.remove_user("g", "u")
    release.set()
    for result in await asyncio.gather(first, second):
        assert result[0]["error_code"] == "vision_context_removed"
    assert len(calls) == 1
    assert cache.metadata("g", ["e"]) == cache.select("g", ["e"]) == []


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_another_waiter():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    first = asyncio.create_task(cache.prepare("g", "u", "e", [URL]))
    await entered.wait()
    second = asyncio.create_task(cache.prepare("g", "u", "e", [URL]))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_expiry_eviction_byte_limits_and_scope_clear():
    now = [0]
    cache = VisionCache(clock=lambda: now[0], ttl_seconds=10, max_events=2,
                        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=pixels())))
    for event in ["1", "2", "3"]:
        await cache.prepare("g", "u", event, [URL])
    assert cache.select("g", ["1"]) == []
    assert len(cache.select("g", ["2", "3"])) == 2
    cache.clear_scope("unrelated")
    assert len(cache.select("g", ["2", "3"])) == 2
    now[0] = 10
    assert cache.select("g", ["2", "3"]) == [] and cache._bytes == 0
    await cache.prepare("g", "u", "4", [URL])
    cache.clear_scope("g")
    assert cache.metadata("g", ["4"]) == [] and cache._bytes == 0
    small = VisionCache(max_bytes=1, transport=cache.transport)
    assert (await small.prepare("g", "u", "1", [URL]))[0]["error_code"] == "vision_cache_full"
    assert small.select("g", ["1"]) == [] and small._bytes == 0


@pytest.mark.asyncio
async def test_pending_work_is_bounded_and_clear_invalidates_all():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    jobs = [asyncio.create_task(cache.prepare("g", "u", str(i), [URL])) for i in range(32)]
    await entered.wait()
    await asyncio.sleep(0)
    assert (await cache.prepare("g", "u", "excess", [URL]))[0]["error_code"] == "vision_busy"
    assert cache.metadata("g", ["excess"])[0]["error_code"] == "vision_busy"
    cache.clear()
    release.set()
    results = await asyncio.gather(*jobs)
    assert all(row[0]["error_code"] == "vision_context_removed" for row in results)
    assert cache.metadata("g", [str(i) for i in range(32)]) == []


@pytest.mark.asyncio
async def test_following_text_waits_for_earlier_image_without_starting_new_download():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    image = asyncio.create_task(cache.prepare("g", "u", "image-event", [URL]))
    await entered.wait()
    assert cache.metadata("g", ["image-event"])[0]["error_code"] == "vision_pending"
    text = asyncio.create_task(cache.wait_pending("g", ["image-event", "text-event"]))
    await asyncio.sleep(0)
    assert not text.done()
    release.set()
    await asyncio.gather(image, text)
    assert len(calls) == 1
    assert cache.metadata("g", ["image-event"])[0]["status"] == "ready"
    assert cache.select("g", ["image-event", "text-event"])[0]["event_id"] == "image-event"


@pytest.mark.asyncio
async def test_wait_pending_never_waits_for_other_scope_or_unrelated_event():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    image = asyncio.create_task(cache.prepare("other", "u", "e", [URL]))
    await entered.wait()
    await asyncio.wait_for(cache.wait_pending("g", ["e"]), timeout=.2)
    await asyncio.wait_for(cache.wait_pending("other", ["different"]), timeout=.2)
    assert not image.done()
    release.set()
    await image


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["clear", "scope", "member"])
async def test_clear_during_following_text_wait_does_not_restore_picture(invalidate):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    image = asyncio.create_task(cache.prepare("g", "u", "e", [URL]))
    await entered.wait()
    text = asyncio.create_task(cache.wait_pending("g", ["e"]))
    await asyncio.sleep(0)
    if invalidate == "clear":
        cache.clear()
    elif invalidate == "scope":
        cache.clear_scope("g")
    else:
        cache.remove_user("g", "u")
    release.set()
    await asyncio.gather(image, text)
    assert cache.metadata("g", ["e"]) == [] and cache.select("g", ["e"]) == []


@pytest.mark.asyncio
async def test_wait_timeout_and_cancel_do_not_cancel_shared_image_job():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(transport=httpx.MockTransport(handler))
    image = asyncio.create_task(cache.prepare("g", "u", "e", [URL]))
    await entered.wait()
    await cache.wait_pending("g", ["e"], timeout=.001)
    assert not image.done()
    assert cache.metadata("g", ["e"])[0]["error_code"] == "vision_pending"
    text = asyncio.create_task(cache.wait_pending("g", ["e"]))
    await asyncio.sleep(0)
    text.cancel()
    with pytest.raises(asyncio.CancelledError):
        await text
    assert not image.done()
    release.set()
    assert (await image)[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_busy_failure_metadata_respects_cache_event_limit():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    cache = VisionCache(max_events=2, transport=httpx.MockTransport(handler))
    jobs = [asyncio.create_task(cache.prepare("g", "u", str(i), [URL])) for i in range(32)]
    await entered.wait()
    await asyncio.sleep(0)
    for i in range(5):
        assert (await cache.prepare("g", "u", "busy" + str(i), [URL]))[0]["error_code"] == "vision_busy"
    assert len(cache._entries) == 2
    assert cache.metadata("g", ["busy0"]) == []
    assert len(cache.metadata("g", ["busy3", "busy4"])) == 2
    cache.clear()
    release.set()
    await asyncio.gather(*jobs)
