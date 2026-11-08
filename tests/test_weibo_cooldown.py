"""Persistent admission/backoff tests; all platform data and sessions are synthetic."""
import asyncio

import httpx
import pytest

from azusa_bot.bilibili import BilibiliSubscriptions
from azusa_bot.subscription_cooldown import SourceCooldown
from azusa_bot.subscription_sources import Sources, SubscriptionError
from test_weibo_subscriptions import rig as weibo_rig, body as weibo_body
from test_subscriptions import rig as dynamic_rig, body as dynamic_body


@pytest.mark.parametrize("code", ["weibo_access_rejected", "weibo_rate_limited", "weibo_login_required"])
def test_weibo_restriction_backoff_survives_restart_and_resets_on_success(tmp_path, code):
    now = [1000.0]
    limiter = SourceCooldown(tmp_path, clock=lambda: now[0])
    for delay in (900, 1800, 3600, 3600):
        limiter.claim("weibo:456")
        failure = limiter.failed("weibo:456", SubscriptionError(code))
        assert failure.retry_after_seconds == delay
        limiter = SourceCooldown(tmp_path, clock=lambda: now[0])
        with pytest.raises(SubscriptionError, match="^weibo_cooldown$") as waiting:
            limiter.claim("weibo:456")
        assert waiting.value.retry_at == now[0] + delay
        now[0] += delay
    limiter.claim("weibo:456")
    limiter.success("weibo:456")
    limiter.claim("weibo:456")
    assert limiter.failed("weibo:456", SubscriptionError(code)).retry_after_seconds == 900


def test_two_request_operation_has_one_long_enough_lease_and_platforms_are_isolated(tmp_path):
    now = [1000.0]
    limiter = SourceCooldown(tmp_path, clock=lambda: now[0])
    limiter.claim("weibo:456")
    now[0] += 40
    restarted = SourceCooldown(tmp_path, clock=lambda: now[0])
    with pytest.raises(SubscriptionError, match="^weibo_request_in_progress$") as waiting:
        restarted.claim("weibo:456")
    assert waiting.value.retry_after_seconds == 20
    restarted.claim("weibo:789")
    restarted.claim("bilibili_dynamic:456")
    restarted.release("weibo:456")
    restarted.claim("weibo:456")


async def test_weibo_preview_create_restart_share_auth_failure_pause(weibo_rig):
    service, now, _, calls, failure = weibo_rig
    failure[0] = httpx.Response(432, text="NEVER_EXPOSE_UPSTREAM_BODY")
    request = {"type": "weibo", "input_type": "uid", "value": "456", "include_image": False}
    with pytest.raises(SubscriptionError, match="^weibo_login_required$") as first:
        await service.preview(request)
    assert first.value.retry_after_seconds == 900
    with pytest.raises(SubscriptionError, match="^weibo_cooldown$"):
        await service.add(weibo_body())
    restarted = BilibiliSubscriptions(service.root, client=service.client, clock=lambda: now[0])
    with pytest.raises(SubscriptionError, match="^weibo_cooldown$"):
        await restarted.preview(request)
    assert len(calls) == 1
    assert restarted.list() == []
    assert "NEVER_EXPOSE_UPSTREAM_BODY" not in str(first.value.public_details())
    failure[0] = None
    now[0] += 901
    assert (await restarted.add(weibo_body()))["enabled"]
    assert restarted.fetch_claim("10001") == []


async def test_weibo_concurrent_read_is_not_duplicated_and_cancel_releases_lease(weibo_rig, monkeypatch):
    service, now, _, calls, _ = weibo_rig
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.sources.request
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(service.sources, "request", delayed)
    task = asyncio.create_task(service.sources.weibo("456", now=now[0]))
    await asyncio.wait_for(entered.wait(), 1)
    second = Sources(service.root, service.client, clock=lambda: now[0])
    with pytest.raises(SubscriptionError, match="^weibo_request_in_progress$"):
        await second.weibo("456", now=now[0])
    assert calls == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await second.weibo("456", now=now[0]))["uid"] == "456"
    assert len(calls) == 2


@pytest.mark.parametrize("code,delay", [("weibo_cooldown", 150), ("weibo_request_in_progress", 60)])
async def test_poll_waits_for_existing_pause_without_counting_another_failure(weibo_rig, monkeypatch, code, delay):
    service, now, _, _, _ = weibo_rig
    sub = await service.add(weibo_body())
    with service.db() as db:
        db.execute("UPDATE rooms SET failures=3")
    now[0] += 301
    async def waiting(_):
        raise SubscriptionError(code, retry_at=now[0]+delay, retry_after_seconds=delay)
    monkeypatch.setattr(service, "_read_existing", waiting)
    assert await service.poll_once() == {"checked": 0}
    with service.db() as db:
        state = dict(db.execute("SELECT failures,next_poll FROM rooms").fetchone())
    assert state == {"failures": 3, "next_poll": now[0] + delay}
    assert service.list()[0]["last_session"] == sub["last_session"]
    assert service.fetch_claim("10001") == []


async def test_poll_real_failure_still_increases_backoff_and_preserves_baseline(weibo_rig, monkeypatch):
    service, now, _, _, _ = weibo_rig
    sub = await service.add(weibo_body())
    now[0] += 301
    async def rejected(_):
        raise SubscriptionError("weibo_login_required", retry_at=now[0]+900, retry_after_seconds=900)
    monkeypatch.setattr(service, "_read_existing", rejected)
    await service.poll_once()
    with service.db() as db:
        state = dict(db.execute("SELECT failures,next_poll FROM rooms").fetchone())
    assert state == {"failures": 1, "next_poll": now[0] + 900}
    assert service.list()[0]["last_session"] == sub["last_session"]
    assert service.fetch_claim("10001") == []


async def test_dynamic_poll_during_another_read_resumes_at_lease_deadline(dynamic_rig):
    service, now, _, _, calls, _ = dynamic_rig
    sub = await service.add(dynamic_body())
    now[0] += 301
    service.sources.cooldown.claim("bilibili_dynamic:456")
    before = len(calls)
    await service.poll_once()
    with service.db() as db:
        state = dict(db.execute("SELECT failures,next_poll,last_error FROM rooms").fetchone())
    assert state == {"failures": 0, "next_poll": now[0] + 60,
                     "last_error": "bilibili_dynamic_request_in_progress"}
    assert len(calls) == before
    assert service.list()[0]["last_session"] == sub["last_session"]
