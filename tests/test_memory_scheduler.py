"""Scheduled extraction processes only current authorized QQ data."""
import asyncio
import json
import time

import pytest

from azusa_bot.core import Engine
from azusa_bot.settings import Settings, write_settings


class NoNetworkExtractor:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, summary=False):
        assert summary
        self.calls.append(json.loads(messages[-1]["content"]))
        return {"memories": []}


@pytest.fixture
def scheduled(tmp_path):
    (tmp_path / "content").mkdir()
    (tmp_path / "content/persona.md").write_text("合成测试角色，不发实际QQ消息。")
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
        onebot_allowed_groups=["30001"], onebot_allowed_dm_users=["20001"], auto_summary_every=4)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    provider = NoNetworkExtractor()
    engine.provider_override = provider
    yield engine, settings, provider
    engine.memory.close()


def pending(engine, scope, user="20001", count=4):
    engine.memory.set_consent(scope, user, True)
    for i in range(count):
        engine.memory.add_history(scope, user, "user", f"合成用户偏好 {i}", f"test-{i}")


@pytest.mark.asyncio
async def test_sparse_member_processed_after_max_wait_and_saved_fact_is_retrievable(scheduled):
    engine,settings,_=scheduled
    settings.auto_summary_every=12;settings.auto_summary_max_wait_minutes=30
    write_settings(engine.root,settings)
    scope="onebot:10001:group:30001"
    pending(engine,scope,count=1)
    with engine.memory._db:
        engine.memory._db.execute("UPDATE history SET text=?,created_at=?",("我喜欢蓝色",time.time()-1801))
    class Extractor:
        calls=0
        async def complete(self,messages,**kwargs):
            self.calls+=1
            rows=json.loads(messages[-1]["content"])
            return {"memories":[{"text":"用户自述喜欢蓝色","source_ids":[rows[0]["id"]]}]}
    extractor=Extractor();engine.provider_override=extractor
    result=await engine.summarize_due()
    assert result["status"]=="saved" and result["saved"]==1
    assert engine.memory.retrieve(scope,"20001","蓝色")[0]["source"]=="inferred"
    assert engine.memory.retrieve(scope,"20002","蓝色")==[]
    await engine.summarize_due()
    assert extractor.calls==1
    status=engine.memory.collection_status(12,30)["members"][0]
    assert status["pending"]==0 and status["summary"]["last_success_at"]


@pytest.mark.asyncio
async def test_sparse_member_age_does_not_bypass_consent_or_scope(scheduled):
    engine,_,provider=scheduled
    scope="onebot:10001:group:30001"
    pending(engine,scope,count=1)
    with engine.memory._db:engine.memory._db.execute("UPDATE history SET created_at=?",(time.time()-1801,))
    assert engine.memory.begin_summary(scope,"20001",threshold=12,max_wait_seconds=3600)["status"]=="below_threshold"
    assert engine.memory.collection_status(12,30)["members"][0]["summary_wait_seconds"]==0
    engine.memory.set_consent(scope,"20001",False)
    await engine.summarize_due()
    assert provider.calls==[]


@pytest.mark.asyncio
async def test_missing_memory_array_is_retryable_not_an_empty_summary(scheduled):
    engine,_,_=scheduled;scope="onebot:10001:group:30001"
    pending(engine,scope)
    class BadExtractor:
        async def complete(self,*args,**kwargs):return {"text":"我处理完了"}
    engine.provider_override=BadExtractor()
    result=await engine.summarize_due()
    assert result["status"]=="summary_invalid"
    status=engine.memory.collection_status()["members"][0]
    assert status["pending"]==4 and status["summary"]["last_success_at"] is None


@pytest.mark.asyncio
async def test_scheduler_ignores_demo_other_bot_unlisted_and_wrong_dm_member(scheduled):
    engine, _, provider = scheduled
    for scope, user in [("demo:group:g1", "20001"),
                        ("onebot:99999:group:30001", "20001"),
                        ("onebot:10001:group:30002", "20001"),
                        ("onebot:10001:dm:20001", "20002"),
                        ("qq:10001:group:30001", "20001")]:
        pending(engine, scope, user)
    await engine.summarize_due()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_scheduler_one_due_member_per_tick_threshold_and_backoff(scheduled):
    engine, _, provider = scheduled
    pending(engine, "onebot:10001:group:30001", "20002")
    pending(engine, "onebot:10001:dm:20001")
    pending(engine, "onebot:10001:group:30001", "20003", count=3)
    await engine.summarize_due()
    assert len(provider.calls) == 1
    await engine.summarize_due()
    assert len(provider.calls) == 2
    await engine.summarize_due()
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_scheduler_disabled_transport_or_profile_does_not_call_provider(scheduled):
    engine, settings, provider = scheduled
    pending(engine, "onebot:10001:dm:20001")
    settings.onebot_enabled = False
    write_settings(engine.root, settings)
    await engine.summarize_due()
    assert provider.calls == []
    settings.onebot_enabled = True
    write_settings(engine.root, settings)
    engine._active_guard = lambda: False
    await engine.summarize_due()
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["whitelist", "transport"])
async def test_scheduler_rechecks_scope_after_waiting_for_capacity(scheduled, revocation):
    engine, settings, provider = scheduled
    scope = "onebot:10001:group:30001"
    pending(engine, scope)

    class CapacityGate:
        def __init__(self):
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def __aenter__(self):
            self.entered.set()
            await self.release.wait()

        async def __aexit__(self, *_):
            return False

    gate = CapacityGate()
    engine._capacity = gate
    task = asyncio.create_task(engine.summarize_due())
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=2)
        if revocation == "whitelist":
            settings.onebot_allowed_groups = []
        else:
            settings.onebot_enabled = False
        write_settings(engine.root, settings)
        gate.release.set()
        await asyncio.wait_for(task, timeout=2)
        assert provider.calls == []
        assert len(engine.memory.summary_candidates(scope, "20001")) == 4
    finally:
        gate.release.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
