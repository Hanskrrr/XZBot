"""Whole-bot shutdown revokes queued and in-flight notification work."""
import asyncio

import pytest

from azusa_bot.bot_control import BotPowerController
from azusa_bot.subscription_sources import SubscriptionError
from test_subscriptions import rig, body, dynamic, tick


async def noop():
    return None


def controller(service):
    power = BotPowerController(service.root, service.cancel_pending)
    power.runtime_start = power.runtime_stop = noop
    return power


@pytest.mark.asyncio
async def test_stop_cancels_claims_and_resume_has_quiet_baseline(rig):
    service, now, feed, _, calls, _ = rig
    subscription = await service.add(body())
    feed.append(dynamic(101, now[0]))
    await tick(rig)
    claimed = service.fetch_claim("10001")[0]
    power = controller(service)
    await power.stop()
    assert not service.authorize_claim(claimed["id"], claimed["claim_token"], "10001")
    assert service.fetch_claim("10001") == []
    before = len(calls)
    await tick(rig)
    assert len(calls) == before
    feed.append(dynamic(102, now[0]))
    await power.start()
    await tick(rig)
    assert service.fetch_claim("10001") == []
    feed.append(dynamic(103, now[0]))
    await tick(rig)
    fresh = service.fetch_claim("10001")
    assert len(fresh) == 1 and fresh[0]["session"] == "103"
    assert service.list()[0]["id"] == subscription["id"]
    assert service.list()[0]["enabled"] is True


@pytest.mark.asyncio
async def test_old_poll_cannot_commit_after_stop_then_start(rig):
    service, now, feed, *_ = rig
    await service.add(body())
    entered, release = asyncio.Event(), asyncio.Event()
    original = service._read_existing
    async def delayed(room):
        result = await original(room)
        entered.set()
        await release.wait()
        return result
    service._read_existing = delayed
    feed.append(dynamic(101, now[0]))
    now[0] += 301
    task = asyncio.create_task(service.poll_once())
    await entered.wait()
    power = controller(service)
    await power.stop()
    await power.start()
    release.set()
    assert await task == {"checked": 0}
    assert service.fetch_claim("10001") == []
    with service.db() as db:
        assert db.execute("SELECT needs_rebaseline FROM subscriptions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_restarted_service_keeps_closed_state(rig):
    from azusa_bot.bilibili import BilibiliSubscriptions
    service, *_ = rig
    await service.add(body())
    await controller(service).stop()
    reopened = BilibiliSubscriptions(service.root, client=service.client, clock=service.clock)
    assert await reopened.poll_once() == {"checked": 0}
    assert reopened.fetch_claim("10001") == []
