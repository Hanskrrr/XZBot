"""Realistic synthetic post shapes; never read real subscriptions or accounts."""
from copy import deepcopy

import httpx
import pytest

from azusa_bot.subscription_sources import Sources, SubscriptionError, _dynamic_card, _weibo_card


def bili_post(*, text="第一段\n第二段", major=None, **extra):
    return {"id_str": "101", "modules": {
        "module_author": {"mid": 456, "name": "合成作者", "pub_ts": 1700000000},
        "module_dynamic": {"desc": {"text": text}, "major": major or {}},
    }, **extra}


def bili_images(count=3):
    return ["https://i0.hdslb.com/bfs/new_dyn/synthetic-" + str(i) + ".jpg" for i in range(count)]


def weibo_images(count=3):
    return ["https://wx1.sinaimg.cn/large/synthetic-" + str(i) + ".jpg" for i in range(count)]


@pytest.mark.parametrize("major", [
    {"draw": {"items": [{"src": url} for url in bili_images()]}},
    {"opus": {"title": "合成图文标题", "summary": {"text": "平台正文\n第二段"},
              "pics": [{"url": url} for url in bili_images()]}},
    {"article": {"title": "合成专栏标题", "covers": bili_images()}},
])
def test_bili_all_picture_variants_preserve_order_and_paragraphs(major):
    original = bili_post(major=major)
    snapshot = deepcopy(original)
    block = _dynamic_card(original)["blocks"][0]
    assert block["text"] == "第一段\n第二段"
    assert block["images"] == bili_images()
    assert block["images_total"] == 3
    assert block["truncated"] is ("article" in major)
    assert original == snapshot


def test_bili_opus_summary_and_video_caption_are_not_replaced_by_title():
    block = _dynamic_card(bili_post(text="", major={"opus": {
        "title": "标题", "summary": {"text": "原文一\n原文二"}}}))["blocks"][0]
    assert (block["title"], block["text"]) == ("标题", "原文一\n原文二")
    block = _dynamic_card(bili_post(major={"archive": {
        "title": "视频标题", "cover": bili_images()[0]}}))["blocks"][0]
    assert block["title"] == "视频标题" and block["text"] == "第一段\n第二段"
    assert block["images"] == bili_images()[:1]


def test_bili_forward_separates_people_and_applies_total_picture_budget():
    original = bili_post(text="被转发者原文", major={"draw": {
        "items": [{"src": url} for url in bili_images(12)]}})
    original["modules"]["module_author"]["name"] = "原作者"
    # An arbitrary chain is intentionally not recursively expanded.
    original["orig"] = bili_post(text="不递归加载")
    post = bili_post(text="转发者评论", orig=original, major={"draw": {
        "items": [{"src": "https://i1.hdslb.com/bfs/new_dyn/own.jpg"}]}})
    blocks = _dynamic_card(post)["blocks"]
    assert len(blocks) == 2
    assert blocks[0]["text"] == "转发者评论"
    assert blocks[1]["label"] == "转发原文 · 原作者"
    assert blocks[1]["text"] == "被转发者原文"
    assert len(blocks[0]["images"]) + len(blocks[1]["images"]) == 9
    assert blocks[1]["images_total"] == 12
    assert blocks[1]["truncated"]  # The second-level forward is not silently claimed as complete.


@pytest.mark.parametrize("orig", [None, {}, {"type": "DYNAMIC_TYPE_NONE"}])
def test_missing_bili_forward_is_visible_not_invented(orig):
    blocks = _dynamic_card(bili_post(type="DYNAMIC_TYPE_FORWARD", orig=orig))["blocks"]
    assert len(blocks) == 2 and blocks[1]["truncated"]
    assert "未返回" in blocks[1]["text"]


def test_source_text_and_malformed_picture_fields_are_bounded():
    block = _dynamic_card(bili_post(text="字" * 5001, major={"opus": {
        "pics": [None, "bad", {}, {"url": 1}, {"url": "x" * 2049},
                 {"url": bili_images()[0]}, {"url": bili_images()[0]}]}}))["blocks"][0]
    assert len(block["text"]) == 5000 and block["truncated"]
    assert block["images"] == bili_images()[:1] and block["images_total"] == 1
    assert _dynamic_card(bili_post(text={"bad": True}, major={"draw": {"items": "bad"}}))["blocks"][0]["text"] == ""


def test_weibo_html_paragraphs_alt_and_links_are_display_text_only():
    html = '<p>第一段<br>下一行<img alt="[微笑]" src="https://evil.test/a"></p><p>第二段 <a href="https://evil.test">原文字样</a></p><script>禁止显示</script><style>禁止显示</style>'
    block = _weibo_card({"text": html})["blocks"][0]
    assert block["text"] == "第一段\n下一行[微笑]\n\n第二段 原文字样"
    assert not block["images"] and not block["truncated"]


@pytest.mark.parametrize("container", ["nested", "flat"])
def test_weibo_embedded_long_text_used_without_an_additional_request(container):
    post = {"text": "被截断的短摘要", "isLongText": True}
    long_text = {"longTextContent": "完整正文<br>最后一段"}
    post.update({"longText": long_text} if container == "nested" else long_text)
    block = _weibo_card(post)["blocks"][0]
    assert block["text"] == "完整正文\n最后一段" and not block["truncated"]
    short = _weibo_card({"text": "平台只返回了摘要", "isLongText": True})["blocks"][0]
    assert short["text"] == "平台只返回了摘要" and short["truncated"]


def test_weibo_forward_keeps_original_body_and_every_available_photo():
    block = _weibo_card({"text": "自己的评语", "retweeted_status": {
        "user": {"screen_name": "原作者"}, "text": "原文<br>下一段",
        "pics": [{"large": {"url": url}, "url": "https://wx1.sinaimg.cn/thumb/small.jpg"}
                 for url in weibo_images(11)], "pic_num": 12,
    }})["blocks"]
    assert block[0]["text"] == "自己的评语" and not block[0]["images"]
    assert block[1]["text"] == "原文\n下一段" and block[1]["label"] == "转发原文 · 原作者"
    assert block[1]["images"] == weibo_images(9) and block[1]["images_total"] == 12
    assert _weibo_card({"text": "转发", "retweeted_status": None})["blocks"][1]["truncated"]


async def test_bili_public_source_exposes_rich_latest_card_to_preview(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"items": [bili_post(major={
            "opus": {"pics": [{"url": url} for url in bili_images()]}})]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sources = Sources(tmp_path, client=client)
        async def credentials(names):
            return {key: "synthetic" if key == "bilibili_sessdata" else "" for key in names}
        monkeypatch.setattr(sources, "credentials", credentials)
        result = await sources.dynamic("456")
    assert len(calls) == 1
    event = result["events"][0]
    assert event["card"] == result["card"]
    assert event["card"]["blocks"][0]["images"] == bili_images()
    assert event["cover"] == bili_images()[0]
    assert "第一段 第二段" in event["text"] and event["name"] == "合成作者"


async def read_bili_fixture(tmp_path, monkeypatch, items):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"code": 0, "data": {"items": items}}))) as client:
        sources = Sources(tmp_path, client=client)
        async def credentials(names):
            return {key: "synthetic" if key == "bilibili_sessdata" else "" for key in names}
        monkeypatch.setattr(sources, "credentials", credentials)
        return await sources.dynamic("456")


async def test_bili_opus_negotiation_recovers_body_missing_from_legacy_draw(tmp_path, monkeypatch):
    # Real response structure, synthetic content: without itemOpusStyle a
    # text-only post can arrive as DRAW, desc=null and draw.items=[].
    calls = []
    def handler(request):
        calls.append(request)
        major = {"type": "MAJOR_TYPE_DRAW", "draw": {"id": 1, "items": []}}
        if "itemOpusStyle" in request.url.params.get("features", "").split(","):
            major = {"type": "MAJOR_TYPE_OPUS", "archive": None, "draw": None,
                "opus": {"title": "", "summary": {"text": "实际正文\n第二段"}, "pics": []}}
        item = bili_post(text="", type="DYNAMIC_TYPE_DRAW", major=major)
        item["modules"]["module_dynamic"]["desc"] = None
        return httpx.Response(200, json={"code": 0, "data": {"items": [item]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sources = Sources(tmp_path, client=client)
        async def credentials(names):
            return {key: "synthetic" if key == "bilibili_sessdata" else "" for key in names}
        monkeypatch.setattr(sources, "credentials", credentials)
        result = await sources.dynamic("456")
    assert len(calls) == 1
    assert result["events"][0]["card"]["blocks"][0]["text"] == "实际正文\n第二段"


@pytest.mark.parametrize("kind", ["DYNAMIC_TYPE_LIVE_RCMD", "DYNAMIC_TYPE_LIVE",
    "DYNAMIC_TYPE_AD", "DYNAMIC_TYPE_NONE", "DYNAMIC_TYPE_FUTURE"])
async def test_bili_non_post_feed_items_never_become_dynamic_links(tmp_path, monkeypatch, kind):
    item = bili_post(type=kind, major={"type": "MAJOR_TYPE_LIVE_RCMD",
        "live_rcmd": {"content": '{"live_play_info":{"title":"合成直播"}}'}})
    result = await read_bili_fixture(tmp_path, monkeypatch, [item])
    assert result["events"] == [] and result["session"] == "0"


@pytest.mark.parametrize("major", [{}, {"type": "MAJOR_TYPE_DRAW", "draw": {"items": []}}])
async def test_bili_empty_post_is_an_error_not_a_placeholder(tmp_path, monkeypatch, major):
    item = bili_post(text="", type="DYNAMIC_TYPE_DRAW", major=major)
    with pytest.raises(SubscriptionError, match="^bilibili_dynamic_content_unavailable$"):
        await read_bili_fixture(tmp_path, monkeypatch, [item])


async def test_bili_blocked_post_is_not_a_visible_dynamic(tmp_path, monkeypatch):
    item = bili_post(type="DYNAMIC_TYPE_DRAW", major={"type": "MAJOR_TYPE_BLOCKED",
        "blocked": {"hint_message": "需要访问权限"}})
    result = await read_bili_fixture(tmp_path, monkeypatch, [item])
    assert result["events"] == [] and result["session"] == "0"


async def test_bili_image_only_and_forward_only_posts_remain_visible(tmp_path, monkeypatch):
    image_post = bili_post(text="", type="DYNAMIC_TYPE_DRAW", major={
        "type": "MAJOR_TYPE_OPUS", "opus": {"pics": [{"url": bili_images()[0]}]}})
    forwarded = bili_post(text="", type="DYNAMIC_TYPE_FORWARD", orig=bili_post(text="原文正文"))
    forwarded["id_str"] = "102"
    result = await read_bili_fixture(tmp_path, monkeypatch, [image_post, forwarded])
    assert len(result["events"]) == 2
    assert result["events"][0]["card"]["blocks"][0]["images"] == bili_images()[:1]
    assert result["events"][1]["card"]["blocks"][1]["text"] == "原文正文"


async def test_bili_missing_forward_placeholder_does_not_count_as_source_content(tmp_path, monkeypatch):
    item = bili_post(text="", type="DYNAMIC_TYPE_FORWARD", orig={"type": "DYNAMIC_TYPE_NONE"})
    with pytest.raises(SubscriptionError, match="^bilibili_dynamic_content_unavailable$"):
        await read_bili_fixture(tmp_path, monkeypatch, [item])


async def test_weibo_public_source_exposes_long_text_without_extra_reads(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if "containerid" not in request.url.params:
            return httpx.Response(200, json={"ok": 1, "data": {
                "userInfo": {"id": "456", "screen_name": "合成作者"},
                "tabsInfo": {"tabs": [{"tab_type": "weibo", "containerid": "107603456"}]}}})
        return httpx.Response(200, json={"ok": 1, "data": {"cards": [{"mblog": {
            "id": "101", "user": {"id": "456"}, "text": "短摘要", "isLongText": True,
            "longText": {"longTextContent": "平台原文<br>末尾"}, "created_at": "刚刚",
            "pics": [{"large": {"url": url}} for url in weibo_images()],
        }}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sources = Sources(tmp_path, client=client, clock=lambda: 1700000000)
        async def credentials(names):
            return {key: "" for key in names}
        monkeypatch.setattr(sources, "credentials", credentials)
        result = await sources.weibo("456")
    assert len(calls) == 2
    event = result["events"][0]
    assert event["card"] == result["card"]
    assert event["card"]["blocks"][0]["text"] == "平台原文\n末尾"
    assert not event["card"]["blocks"][0]["truncated"]
    assert event["card"]["blocks"][0]["images"] == weibo_images()
    assert event["cover"] == weibo_images()[0]
