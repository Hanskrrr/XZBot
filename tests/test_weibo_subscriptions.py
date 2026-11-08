"""Synthetic-only Weibo and authenticated QQ subscription command checks."""
import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from azusa_bot.bilibili import BilibiliSubscriptions
from azusa_bot.settings import Settings, Owner, read_settings, write_settings
from azusa_bot.subscription_sources import source_id, SubscriptionError
from azusa_bot.subscription_cards import safe_image_url
from azusa_bot.subscription_credentials import update_credentials, credential_status
from azusa_bot.subscription_commands import handle_subscription_command


def post(number, now, **extra):
    return {"id": str(number), "user": {"id": "456", "screen_name": "测试微博"},
            "text": "合成正文 <a href='https://example.test'>链接文字</a><script>不执行</script>",
            "created_at": format_datetime(datetime.fromtimestamp(now, timezone.utc)),
            "pics": [{"large": {"url": "https://wx1.sinaimg.cn/large/test.png"}}], **extra}


@pytest.fixture
def rig(tmp_path):
    now, calls = [1700000000.0], []
    feed = [post(100, now[0])]
    failure = [None]
    picture = io.BytesIO(); Image.new("RGB", (100, 250), "#778899").save(picture, format="PNG")
    def handler(request):
        calls.append(request)
        if request.url.host == "m.weibo.cn":
            assert "SUB=" not in str(request.url)
            if failure[0]:
                return failure[0]
            if "containerid" not in request.url.params:
                return httpx.Response(200, json={"ok": 1, "data": {"userInfo": {"id": "456", "screen_name": "测试微博", "profile_image_url": "https://tvax1.sinaimg.cn/test.png"},
                    "tabsInfo": {"tabs": [{"tab_type": "weibo", "containerid": "107603456"}]}}})
            return httpx.Response(200, json={"ok": 1, "data": {"cards": [{"mblog": p} for p in feed]}})
        assert request.url.host in {"wx1.sinaimg.cn", "tvax1.sinaimg.cn"}
        assert "cookie" not in request.headers
        return httpx.Response(200, content=picture.getvalue(), headers={"content-type": "image/png"})
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id="10001", onebot_enabled=True,
        onebot_allowed_groups=["20001", "20002"], onebot_allowed_dm_users=["30001"],
        onebot_owners=[Owner(kind="group", scope_id="20001", user_id="30001")]))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = BilibiliSubscriptions(tmp_path, client=client, clock=lambda: now[0])
    return service, now, feed, calls, failure


def body(**extra):
    return {"type": "weibo", "input_type": "uid", "value": "456", "kind": "group", "target_id": "20001",
            "enabled": True, "include_image": False, **extra}


def event(text, **extra):
    return SimpleNamespace(text=text, transport="onebot", app_id="10001", kind="group", scope_id="20001", user_id="30001", **extra)


async def test_weibo_baseline_restart_pinned_and_text_sanitization(rig):
    service, now, feed, calls, _ = rig
    sub = await service.add(body())
    assert sub["url"] == "https://m.weibo.cn/u/456"
    assert all("cookie" not in request.headers for request in calls)
    now[0] += 301; await service.poll_once()
    assert service.fetch_claim("10001") == []
    feed[:] = [post(99, now[0]), post(101, now[0])]
    now[0] += 301; await service.poll_once()
    reply = service.fetch_claim("10001")[0]
    assert reply["type"] == "weibo" and reply["session"] == "101"
    assert "链接文字" in reply["text"] and "<a" not in reply["text"] and "不执行" not in reply["text"]
    service.ack(reply["id"], reply["claim_token"], "sent")
    service = BilibiliSubscriptions(service.root, client=service.client, clock=lambda: now[0])
    feed[:] = [post(50, now[0])]
    now[0] += 301; await service.poll_once()
    assert service.fetch_claim("10001") == []
    assert service.list()[0]["last_session"] == "101"


async def test_weibo_cookie_is_write_only_and_not_forwarded_to_images(rig):
    service, now, feed, calls, _ = rig
    update_credentials(service.root, {"weibo_sub": "synthetic-session-only"})
    assert credential_status(service.root)["fields"]["weibo_sub"] == {"configured": True}
    preview = await service.preview({"type": "weibo", "input_type": "uid", "value": "456"})
    assert Path(preview["image_file"]).exists()
    assert "synthetic-session-only" not in str(preview)
    for request in calls:
        assert "synthetic-session-only" not in str(request.url)
        assert (request.headers.get("cookie") == "SUB=synthetic-session-only") == (request.url.host == "m.weibo.cn")
    update_credentials(service.root, {"weibo_sub": ""})
    assert credential_status(service.root)["fields"]["weibo_sub"]["configured"]
    update_credentials(service.root, {"clear": ["weibo_sub"]})
    assert not credential_status(service.root)["fields"]["weibo_sub"]["configured"]


@pytest.mark.parametrize("bad", [httpx.Response(302, headers={"location": "https://passport.weibo.cn/"}),
    httpx.Response(200, json={"ok": 0, "msg": "private token body"}),
    httpx.Response(200, json={"ok": 1, "data": {}}), httpx.Response(200, text="private HTML challenge")])
async def test_weibo_denial_stops_without_raw_error_or_baseline(rig, bad):
    service, _, _, calls, failure = rig
    failure[0] = bad
    with pytest.raises(SubscriptionError) as caught:
        await service.add(body())
    assert "private" not in str(caught.value) and "token" not in str(caught.value)
    assert service.list() == [] and len(calls) == 1


async def test_wrong_author_cannot_establish_baseline(rig):
    service, _, feed, *_ = rig
    feed[0]["user"]["id"] = "999"
    with pytest.raises(SubscriptionError, match="weibo_response_invalid"):
        await service.add(body())
    assert service.list() == []


async def test_weibo_new_events_capped_and_pause_revokes_card(rig):
    service, now, feed, *_ = rig
    sub = await service.add(body(include_image=True))
    feed[:] = [post(n, now[0]) for n in range(101, 110)] + [post(110, now[0]-10000)]
    now[0] += 301; await service.poll_once()
    assert service.status()["pending"] == 5
    claimed = service.fetch_claim("10001")[0]
    assert Path(claimed["image_file"]).exists()
    await service.update(sub["id"], {"enabled": False})
    assert not service.authorize_claim(claimed["id"], claimed["claim_token"], "10001")


@pytest.mark.parametrize("value", ["456", "https://weibo.com/u/456", "https://www.weibo.com/u/456", "https://m.weibo.cn/u/456", "https://m.weibo.cn/profile/456"])
def test_weibo_numeric_sources(value):
    assert source_id("weibo", value) == "456"


@pytest.mark.parametrize("value", ["https://weibo.com/n/someone", "https://weibo.com@localhost/u/456", "https://m.weibo.cn/u/456?cookie=secret", "https://m.weibo.cn:443/u/456", "https://evil.test/u/456", "https://weibo.com/u/456#anything"])
def test_weibo_rejects_ambiguous_or_foreign_urls(value):
    with pytest.raises(ValueError): source_id("weibo", value)


def test_weibo_image_allowlist():
    assert safe_image_url("http://wx1.sinaimg.cn/large/test.jpg") == "https://wx1.sinaimg.cn/large/test.jpg"
    assert safe_image_url("https://tvax2.sinaimg.cn/test.jpg")
    for url in ("https://weibo.com/test.jpg", "https://wx1.sinaimg.cn.evil.test/a", "https://wx1.sinaimg.cn:443/a", "https://localhost/a"):
        assert not safe_image_url(url)


async def test_commands_current_scope_only_and_image_control(rig):
    service, *_ = rig
    answer = await handle_subscription_command(service, event("/订阅 添加 微博 456 文字"), True)
    assert "已添加" in answer
    row = service.list()[0]
    assert row["target_id"] == "20001" and row["enabled"] and not row["include_image"]
    listing = await handle_subscription_command(service, event("/订阅 列表"), False)
    assert row["id"][:8] in listing
    for command, field, expected in [("图文", "include_image", True), ("暂停", "enabled", False), ("恢复", "enabled", True)]:
        await handle_subscription_command(service, event(f"/订阅 {command} {row['id'][:8]}"), True)
        assert service.list()[0][field] is expected
    await handle_subscription_command(service, event(f"/订阅 删除 {row['id'][:8]}"), True)
    assert service.list() == []


async def test_nonowner_and_cross_scope_cannot_mutate_or_learn_other_targets(rig):
    service, *_ = rig
    first = await service.add(body(target_id="20002"))
    stranger = event("/订阅 添加 微博 456"); stranger.user_id = "30002"
    assert "owner" in await handle_subscription_command(service, stranger, True)
    assert "owner" in await handle_subscription_command(service, event("/订阅 添加 微博 456"), False)
    assert "还没有" in await handle_subscription_command(service, event("/订阅 列表"), False)
    answer = await handle_subscription_command(service, event("/订阅 删除 " + first["id"]), True)
    assert "没有唯一匹配" in answer
    assert len(service.list()) == 1
    assert "格式" in await handle_subscription_command(service, event("/订阅 添加 微博 456 图文 20002"), True)


async def test_scope_and_identity_mismatch_cannot_mutate(rig):
    service, *_ = rig
    for field, value in [("app_id", "10002"), ("transport", "official"), ("scope_id", "99999"), ("user_id", "10001")]:
        item = event("/订阅 添加 微博 456"); setattr(item, field, value)
        assert "暂时不能" in await handle_subscription_command(service, item, True)
    assert service.list() == []


@pytest.mark.parametrize("operation", ["添加", "恢复"])
async def test_owner_revoked_during_source_read_cannot_commit(rig, monkeypatch, operation):
    service, *_ = rig
    number = None
    if operation == "恢复":
        number = (await service.add(body(enabled=False)))["id"]
    started, release = asyncio.Event(), asyncio.Event()
    read = service.sources.weibo
    async def delayed(*args, **kwargs):
        started.set(); await release.wait()
        return await read(*args, **kwargs)
    monkeypatch.setattr(service.sources, "weibo", delayed)
    text = "/订阅 添加 微博 456" if operation == "添加" else "/订阅 恢复 " + number
    pending = asyncio.create_task(handle_subscription_command(service, event(text), True))
    await asyncio.wait_for(started.wait(), 1)
    settings = read_settings(service.root); settings.onebot_owners = []; write_settings(service.root, settings)
    release.set()
    assert "owner" in await pending
    rows = service.list()
    assert rows == [] if operation == "添加" else not rows[0]["enabled"]


async def test_help_is_local_and_does_not_call_sources(rig):
    service, _, _, calls, _ = rig
    assert "微博" in await handle_subscription_command(service, event("/订阅 帮助"), False)
    assert await handle_subscription_command(service, event("普通聊天"), False) is None
    assert calls == []
