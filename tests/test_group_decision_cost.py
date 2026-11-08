"""Busy-group cancellation must not erase the interval of a paid model call.

Synthetic events, local clocks and a fake provider only; no QQ or cloud calls.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from context_helpers import context as wire_context

from azusa_bot.core import Engine, Incoming
from azusa_bot.settings import Settings, private_write, write_settings


SCOPE = "onebot:10001:group:30001"


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Provider:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return {"participate": True, "confidence": .99, "mode": "text",
                "text": "可以一起讨论这个问题。"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    private_write(tmp_path / "content/persona.md", "# 测试角色\n自然简短地回应。")
    clock = Clock()
    monkeypatch.setattr("azusa_bot.core.time", SimpleNamespace(time=clock, monotonic=clock,
                         strftime=time.strftime, gmtime=time.gmtime))
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
        onebot_allowed_groups=["30001"], provider_mode="cloud", group_chat_enabled=True,
        group_chat_debounce_seconds=0, group_chat_min_messages=1,
        group_chat_decision_interval_seconds=30, group_followup_enabled=False,
        group_followup_min_interval_seconds=5, min_interval_seconds=0)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    engine.groups.clock = clock
    engine.address_names_loader = lambda: ["测试角色"]
    provider = Provider()
    engine.provider_override = provider
    yield engine, provider, settings, clock
    engine.vision.clear(); engine.groups.close(); engine.memory.close()


def event(number, text=None):
    return Incoming(transport="onebot", app_id="10001", kind="group", scope_id="30001",
        user_id="20001", event_id=str(number), text=text or f"大家继续讨论第 {number} 条",
        unprompted=True)


async def supersede(engine, provider, first=1, second=2):
    task = asyncio.create_task(engine.handle(event(first)))
    await asyncio.wait_for(provider.entered.wait(), 1)
    newer = asyncio.create_task(engine.handle(event(second)))
    await asyncio.sleep(0)
    assert engine.groups.snapshot(SCOPE)[-1]["message_id"] == str(second)
    provider.release.set()
    return await asyncio.gather(task, newer)


@pytest.mark.asyncio
async def test_superseded_call_keeps_30_second_gap_without_automatic_retry(rig):
    engine, provider, _, clock = rig
    started = clock.now
    old, newer = await supersede(engine, provider)
    assert old["reason"] == "context_changed_during_generation"
    assert newer["reason"] == "decision_cooldown"
    assert len(provider.calls) == 1
    assert engine._group_decisions[SCOPE] == (0., started)
    clock.advance(29)
    assert (await engine.handle(event(3)))["reason"] == "decision_cooldown"
    assert len(provider.calls) == 1
    clock.advance(1)
    await asyncio.sleep(0)
    assert len(provider.calls) == 1  # No timer or queued retry consumes a call.
    assert (await engine.handle(event(4)))["mode"] == "text"
    assert len(provider.calls) == 2
    with engine.ledger.db() as db:
        assert db.execute("SELECT SUM(used) FROM budget").fetchone()[0] == 2
    context = wire_context(provider.calls[-1])
    assert [row["message_id"] for row in context["recent_conversation"]+[context["current_message"]]] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_cancelled_batch_remains_unprocessed_for_minimum_message_count(rig):
    engine, provider, settings, clock = rig
    settings.group_chat_min_messages = 3
    write_settings(engine.root, settings)
    engine.groups.add(SCOPE, "20001", "较早的第一条", "seed-1")
    engine.groups.add(SCOPE, "20002", "较早的第二条", "seed-2")
    await supersede(engine, provider)
    clock.advance(30)
    # Only two messages arrived after the cancelled attempt's initial cutoff.
    # Its entire batch must still count towards the next decision.
    assert (await engine.handle(event(3)))["mode"] == "text"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_name_priority_still_uses_its_own_short_interval(rig, monkeypatch):
    engine, provider, _, clock = rig
    await supersede(engine, provider)
    clock.advance(2)
    assert (await engine.handle(event(3, "测试角色，你怎么看")))["mode"] == "text"
    assert len(provider.calls) == 2
    clock.advance(4)
    from test_followup_scheduling import controlled_wait
    entered, release, delays = controlled_wait(monkeypatch, clock)
    pending = asyncio.create_task(engine.handle(event(4, "测试角色，再说一句")))
    await asyncio.wait_for(entered.wait(), 1)
    assert len(provider.calls) == 2
    release.set()
    assert (await pending)["mode"] == "text"
    assert delays == [1.0]
    assert len(provider.calls) == 3
