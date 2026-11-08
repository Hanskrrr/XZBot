"""Source stability cases based on upstream contracts; no live credentials/HTTP."""
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from azusa_bot.subscription_sources import Sources, SubscriptionError
from test_subscription_source_compatibility import sources, weibo_profile
from test_weibo_subscriptions import post


async def test_indexed_picture_objects_keep_content_and_forward_order(tmp_path, monkeypatch):
    now = 1700000000
    item = post(101, now, pics={
        "0": {"large": {"url": "https://wx1.sinaimg.cn/large/first.jpg"}},
        "1": {"large": {"url": "https://wx2.sinaimg.cn/large/second.jpg"}},
    }, retweeted_status={
        "user": {"screen_name": "合成原作者"}, "text": "原文", "pics": {
            "0": {"large": {"url": "https://wx3.sinaimg.cn/large/original.jpg"}}}})
    def handler(request):
        if "containerid" not in request.url.params:
            return httpx.Response(200, json=weibo_profile())
        return httpx.Response(200, json={"ok": 1, "data": {"cards": [{"mblog": item}]}})
    source = sources(tmp_path, handler, monkeypatch)
    try:
        event = (await source.weibo("456"))["events"][0]
        own, original = event["card"]["blocks"]
        assert own["images"] == ["https://wx1.sinaimg.cn/large/first.jpg", "https://wx2.sinaimg.cn/large/second.jpg"]
        assert original["images"] == ["https://wx3.sinaimg.cn/large/original.jpg"]
        assert event["cover"] == own["images"][0] and own["images_total"] == 2
    finally:
        await source.client.aclose()


@pytest.mark.parametrize("response,expected,pause", [
    (httpx.Response(200, text="<html>private login or challenge response</html>"), "weibo_response_not_json", 900),
    (httpx.Response(200, content=b"invalid private json"), "weibo_response_not_json", 900),
    (httpx.Response(429, headers={"Retry-After": "7200"}), "weibo_rate_limited", 7200),
    (httpx.Response(503, headers={"Retry-After": "120"}), "weibo_http_503", 120),
])
async def test_fixed_response_classification_and_upstream_pause_survive_restart(tmp_path, monkeypatch, response, expected, pause):
    calls=[]
    def handler(request):
        calls.append(request)
        return response
    source=sources(tmp_path,handler,monkeypatch)
    try:
        with pytest.raises(SubscriptionError,match="^"+expected+"$") as error:
            await source.weibo("456")
        assert error.value.retry_after_seconds == pause
        assert "private" not in str(error.value.public_details())
        restarted=Sources(tmp_path,client=source.client,clock=source.clock)
        with pytest.raises(SubscriptionError,match="^weibo_cooldown$") as cooldown:
            # Check admission directly: no real credential storage access.
            restarted.cooldown.claim("weibo:456")
        assert cooldown.value.retry_after_seconds==pause and len(calls)==1
    finally:
        await source.client.aclose()


async def test_network_failure_is_distinct_from_html_and_does_not_leak_request(tmp_path,monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("private upstream token",request=request)
    source=sources(tmp_path,handler,monkeypatch)
    try:
        with pytest.raises(SubscriptionError,match="^weibo_network_error$") as error:
            await source.weibo("456")
        assert error.value.retry_after_seconds==30
        assert "private" not in str(error.value.public_details())
    finally:
        await source.client.aclose()


def test_retry_after_dates_and_bounds(tmp_path):
    now=1700000000
    source=Sources(tmp_path,clock=lambda:now)
    assert source._retry_after(format_datetime(datetime.fromtimestamp(now+7200,timezone.utc)))==7200
    assert source._retry_after(format_datetime(datetime.fromtimestamp(now-100,timezone.utc)))==0
    assert source._retry_after("9999999999")==86400
    for invalid in (None,"-10","1.5","token-in-error","x"*101):
        assert source._retry_after(invalid)==0
