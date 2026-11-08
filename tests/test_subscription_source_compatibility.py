"""Synthetic platform-contract regressions; no real accounts or HTTP calls."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from azusa_bot.subscription_sources import Sources, SubscriptionError, _weibo_time


def dynamic_item(stamp):
    return {"id_str": "100", "modules": {
        "module_author": {"mid": 456, "name": "合成账号", "pub_ts": stamp},
        "module_dynamic": {"desc": {"text": "合成动态"}},
    }}


def sources(tmp_path, handler, monkeypatch, now=None):
    instance = Sources(tmp_path, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                       clock=lambda: now[0] if now is not None else 1700000000)
    async def credentials(names):
        return {name: "synthetic-cookie" if name == "bilibili_sessdata" else "" for name in names}
    monkeypatch.setattr(instance, "credentials", credentials)
    return instance


@pytest.mark.parametrize("stamp", [1700000000, "1700000000", "0", 0])
async def test_bilibili_decimal_timestamp_preserves_exact_publication_time(tmp_path, monkeypatch, stamp):
    source = sources(tmp_path, lambda request: httpx.Response(200, json={
        "code": 0, "data": {"items": [dynamic_item(stamp)]}}), monkeypatch)
    try:
        result = await source.dynamic("456")
        assert result["events"][0]["published_at"] == int(stamp)
        assert type(result["events"][0]["published_at"]) is int
        assert result["session"] == "100"
    finally:
        await source.client.aclose()


@pytest.mark.parametrize("stamp", [True, False, 1700000000.5, "1e9", "1700000000.5", "-1", " 1700000000", "now", "９９９", "9999999999999", 253402300800])
async def test_bilibili_unknown_timestamp_never_becomes_a_fresh_event(tmp_path, monkeypatch, stamp):
    source = sources(tmp_path, lambda request: httpx.Response(200, json={
        "code": 0, "data": {"items": [dynamic_item(stamp)]}}), monkeypatch)
    try:
        with pytest.raises(SubscriptionError, match="^bilibili_dynamic_response_invalid$"):
            await source.dynamic("456")
    finally:
        await source.client.aclose()


def weibo_profile():
    return {"ok": 1, "data": {"userInfo": {"id": "456", "screen_name": "合成账号"},
        "tabsInfo": {"tabs": [{"tab_type": "weibo", "containerid": "107603456"}]}}}


@pytest.mark.parametrize("denial,code", [
    (httpx.Response(432, text="synthetic private body"), "weibo_login_required"),
    (httpx.Response(302, headers={"location": "https://passport.weibo.cn/"}), "weibo_login_required"),
    (httpx.Response(200, json={"ok": -100, "msg": "synthetic private body"}), "weibo_login_required"),
    (httpx.Response(403), "weibo_access_rejected"),
    (httpx.Response(429), "weibo_rate_limited"),
])
async def test_weibo_expired_or_rejected_session_has_actionable_fixed_error(tmp_path, monkeypatch, denial, code):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["MWeibo-Pwa"] == "1"
        return denial
    source = sources(tmp_path, handler, monkeypatch)
    try:
        with pytest.raises(SubscriptionError, match="^"+code+"$") as error:
            await source.weibo("456")
        assert error.value.retry_after_seconds == 900
        with pytest.raises(SubscriptionError, match="^weibo_cooldown$"):
            await source.weibo("456")
        assert len(calls) == 1
    finally:
        await source.client.aclose()


async def test_weibo_profile_cache_reduces_reads_and_invalidates_on_cookie_change(tmp_path, monkeypatch):
    now, calls, cookie = [1700000000.0], [], [""]
    def handler(request):
        calls.append(request)
        if "containerid" not in request.url.params:
            return httpx.Response(200, json=weibo_profile())
        return httpx.Response(200, json={"ok": 1, "data": {"cards": []}})
    source = sources(tmp_path, handler, monkeypatch, now)
    async def credentials(names):
        return {name: cookie[0] if name == "weibo_sub" else "" for name in names}
    monkeypatch.setattr(source, "credentials", credentials)
    try:
        await source.weibo("456")
        assert len(calls) == 2
        now[0] += 300
        await source.weibo("456")
        assert len(calls) == 3
        cookie[0] = "synthetic-new-session"
        await source.weibo("456")
        assert len(calls) == 5
        now[0] += 3601
        await source.weibo("456")
        assert len(calls) == 7
    finally:
        await source.client.aclose()


async def test_weibo_complete_session_cookie_preferred_and_never_followed_to_other_host(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "m.weibo.cn"
        assert request.headers["cookie"] == "SUB=synthetic-session; SUBP=synthetic-support"
        return httpx.Response(302, headers={"location": "https://example.test/never-visit"})
    source = sources(tmp_path, handler, monkeypatch)
    async def credentials(names):
        values = {"weibo_cookie": "SUB=synthetic-session; SUBP=synthetic-support", "weibo_sub": "legacy-synthetic"}
        return {name: values[name] for name in names}
    monkeypatch.setattr(source, "credentials", credentials)
    try:
        with pytest.raises(SubscriptionError, match="^weibo_login_required$"):
            await source.weibo("456")
        assert len(calls) == 1
    finally:
        await source.client.aclose()


def test_weibo_relative_and_calendar_dates_keep_china_timezone():
    local = datetime(2026, 1, 1, 0, 5, tzinfo=timezone(timedelta(hours=8)))
    now = local.timestamp()
    assert _weibo_time("昨天 23:58", now) == now-7*60
    assert _weibo_time("今天 00:02", now) == now-3*60
    assert _weibo_time("12-31 23:58", now) == now-7*60
    assert _weibo_time("2025-12-31 23:58", now) == now-7*60
    assert _weibo_time("12-31", now) == int(datetime(2025, 12, 31, tzinfo=local.tzinfo).timestamp())
    for invalid in ("昨天 25:00", "今天 00:99", "时间未知"):
        with pytest.raises(ValueError):
            _weibo_time(invalid, now)
