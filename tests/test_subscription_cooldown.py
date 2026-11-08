"""Use synthetic Bilibili responses; never read real cookies or call platforms."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from azusa_bot.bilibili import BilibiliSubscriptions
from azusa_bot.subscription_sources import Sources, SubscriptionError
from azusa_bot.subscription_credentials import update_credentials


def empty_feed():
    return {"code": 0, "data": {"items": []}}


@pytest.fixture
def rig(tmp_path):
    now, calls, response = [1000.0], [], [httpx.Response(412, text="NEVER_STORE_UPSTREAM_BODY")]
    def transport(request):
        calls.append(request)
        return response[0]
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    source = Sources(tmp_path, client, clock=lambda: now[0])
    update_credentials(tmp_path, {"bilibili_sessdata": "synthetic-session"})
    return tmp_path, source, client, now, calls, response


async def test_rejection_is_safe_and_shared_with_restart_and_other_source_isolated(rig):
    root, source, client, now, calls, _ = rig
    with pytest.raises(SubscriptionError) as first:
        await source.dynamic("123")
    assert first.value.public_details() == {"detail": "bilibili_dynamic_access_rejected", "retry_at": 1900.0, "retry_after_seconds": 900}
    restarted = Sources(root, client, clock=lambda: now[0])
    now[0] += 1
    with pytest.raises(SubscriptionError) as repeat:
        await restarted.dynamic("123")
    assert str(repeat.value) == "bilibili_dynamic_cooldown"
    assert repeat.value.retry_after_seconds == 899
    assert len(calls) == 1
    with pytest.raises(SubscriptionError):
        await restarted.dynamic("456")
    assert len(calls) == 2
    with sqlite3.connect(root / "data/bilibili.sqlite3") as db:
        saved = json.dumps(db.execute("SELECT * FROM subscription_source_cooldowns").fetchall())
    assert "synthetic-session" not in saved and "NEVER_STORE_UPSTREAM_BODY" not in saved


async def test_preview_and_add_share_failed_source_cooldown(rig):
    root, _, client, now, calls, _ = rig
    from azusa_bot.settings import Settings, write_settings
    write_settings(root, Settings(transport="onebot", onebot_self_id="10001", onebot_allowed_groups=["20001"]))
    service = BilibiliSubscriptions(root, client=client, clock=lambda: now[0])
    body = {"type": "bilibili_dynamic", "input_type": "uid", "value": "123", "include_image": False}
    with pytest.raises(SubscriptionError):
        await service.preview(body)
    with pytest.raises(SubscriptionError, match="bilibili_dynamic_cooldown"):
        await service.add(body | {"kind": "group", "target_id": "20001"})
    assert len(calls) == 1
    assert service.list() == []


async def test_backoff_increases_and_success_resets(rig):
    _, source, _, now, calls, response = rig
    for delay in (900, 1800, 3600, 3600):
        with pytest.raises(SubscriptionError) as rejected:
            await source.dynamic("123")
        assert rejected.value.retry_after_seconds == delay
        now[0] += delay + 1
    response[0] = httpx.Response(200, json=empty_feed())
    assert (await source.dynamic("123"))["events"] == []
    response[0] = httpx.Response(412)
    with pytest.raises(SubscriptionError) as fresh:
        await source.dynamic("123")
    assert fresh.value.retry_after_seconds == 900
    assert len(calls) == 6


async def test_background_poll_respects_source_pause_and_preserves_baseline(rig):
    root, _, client, now, calls, response = rig
    from azusa_bot.settings import Settings, write_settings
    write_settings(root, Settings(transport="onebot", onebot_self_id="10001", onebot_allowed_groups=["20001"]))
    service = BilibiliSubscriptions(root, client=client, clock=lambda: now[0])
    response[0] = httpx.Response(200, json=empty_feed())
    body = {"type": "bilibili_dynamic", "input_type": "uid", "value": "123", "include_image": False,
            "kind": "group", "target_id": "20001", "enabled": True}
    await service.add(body)
    response[0] = httpx.Response(412)
    now[0] += 301
    await service.poll_once()
    row = service.list()[0]
    assert row["next_poll"] == now[0] + 900
    assert row["last_session"] == "0"
    before = len(calls)
    now[0] += 601
    await service.poll_once()
    assert len(calls) == before


@pytest.mark.parametrize("body,code,delay", [
    ({"code": -352}, "bilibili_dynamic_access_rejected", 900),
    ({"code": -412}, "bilibili_dynamic_access_rejected", 900),
    ({"code": -509}, "bilibili_dynamic_rate_limited", 900),
    ({"code": -799}, "bilibili_dynamic_rate_limited", 900),
    ({"code": -101}, "bilibili_dynamic_login_required", 30),
])
async def test_body_rejection_is_not_misrepresented_as_missing_optional_cookie(rig, body, code, delay):
    _, source, _, _, _, response = rig
    response[0] = httpx.Response(200, json=body)
    with pytest.raises(SubscriptionError) as rejected:
        await source.dynamic("123")
    assert str(rejected.value) == code
    assert rejected.value.retry_after_seconds == delay


async def test_same_source_concurrent_request_does_not_double_fetch(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def transport(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=empty_feed())
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    first, second = Sources(tmp_path, client), Sources(tmp_path, client)
    update_credentials(tmp_path, {"bilibili_sessdata": "synthetic-session"})
    request = asyncio.create_task(first.dynamic("123"))
    await entered.wait()
    with pytest.raises(SubscriptionError, match="bilibili_dynamic_request_in_progress"):
        await second.dynamic("123")
    release.set()
    await request
    assert len(calls) == 1


async def test_cancellation_releases_request_lease(tmp_path):
    entered = asyncio.Event()
    async def transport(request):
        entered.set()
        await asyncio.Event().wait()
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    source = Sources(tmp_path, client)
    update_credentials(tmp_path, {"bilibili_sessdata": "synthetic-session"})
    request = asyncio.create_task(source.dynamic("123"))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    source.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=empty_feed())))
    assert (await source.dynamic("123"))["events"] == []


async def test_request_shape_is_explicit_json_without_browser_fingerprint(rig):
    _, source, _, _, calls, response = rig
    response[0] = httpx.Response(200, json=empty_feed())
    await source.dynamic("123")
    request = calls[0]
    assert request.headers["accept"] == "application/json, text/plain, */*"
    assert request.headers["referer"] == "https://space.bilibili.com/123/dynamic"
    assert request.url.params["platform"] == "web"
    assert request.headers["user-agent"].startswith("XZBot/")
    assert not any(key.startswith("dm_img") for key in request.url.params)
