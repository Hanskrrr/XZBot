"""Synthetic request fitting, explicit overflow recovery, and summary progress."""
import asyncio
import json
import time

import pytest

from azusa_bot.context_budget import estimate_tokens
from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import ProviderError
from azusa_bot.settings import Settings, private_write, write_settings
from azusa_bot.trajectories import references

SCOPE = "onebot:10001:dm:20001"


class Provider:
    def __init__(self):
        self.calls = []
        self.errors = []
        self.entered = self.release = None

    async def complete(self, messages, *, summary=False):
        self.calls.append((messages,summary))
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.errors:
            code = self.errors.pop(0)
            if code:
                raise ProviderError(code)
        if summary:
            rows = json.loads(messages[-1]["content"])
            return {"memories":[{"text":"用户自述喜欢合成测试", "source_ids":[rows[0]["id"]]}]}
        return {"mode":"text","text":"合成回答"}


@pytest.fixture
def rig(tmp_path):
    private_write(tmp_path / "content/persona.md", "这是完整的合成核心人设，不应被截断。")
    settings = Settings(transport="onebot",onebot_enabled=True,onebot_self_id="10001",
        onebot_allowed_dm_users=["20001"],provider_mode="cloud",min_interval_seconds=0,
        auto_summary_every=100,context_window_tokens=32768,daily_cloud_requests=10)
    write_settings(tmp_path,settings)
    engine = Engine(tmp_path)
    engine.memory.set_consent(SCOPE,"20001",True)
    provider = Provider()
    engine.provider_override = provider
    yield engine,provider,settings
    engine.memory.close(); engine.groups.close()


def event():
    return Incoming(transport="onebot",app_id="10001",kind="dm",scope_id="20001",
                    user_id="20001",event_id="current",text="继续解释合成测试")


def seed(engine, pairs=7):
    for i in range(pairs):
        engine.dialogue.add(dict(scope=SCOPE,user_id="20001",self_id="10001",event_id=f"old-{i}",
            user_text=f"旧问题{i}："+"甲"*600,user_at=time.monotonic(),text=f"旧回答{i}："+"乙"*600,mode="text"),f"reply-{i}")
        engine.memory.add_history(SCOPE,"20001","user",f"旧问题{i}："+"甲"*600,f"old-{i}")
        engine.memory.add_history(SCOPE,"20001","assistant",f"旧回答{i}："+"乙"*600,f"old-{i}:reply")


def used(engine):
    with engine.ledger.db() as db:
        return db.execute("SELECT COALESCE(SUM(used),0) FROM budget").fetchone()[0]


@pytest.mark.asyncio
async def test_actual_prompt_fits_and_trace_tracks_retained_history(rig):
    engine, provider, settings = rig
    seed(engine)
    before = engine.memory.stats()["history"]
    result = await engine.handle(event())
    assert result["mode"] == "text" and len(provider.calls) == 1 and used(engine) == 1
    messages = provider.calls[0][0]
    assert estimate_tokens(messages)+1200+1024 <= settings.context_window_tokens
    assert "完整的合成核心人设" in messages[0]["content"]
    assert messages[-1]["content"] == event().text
    trace = engine.trajectories.list()["items"][0]
    assert trace["context_budget"]["trimmed"] and trace["context_budget"]["removed"]["dialogue"] > 0
    assert trace["counts"]["dialogue"] == len(json.loads(messages[1]["content"].split("\n",1)[1])["confirmed_direct_dialogue"])
    assert engine.memory.stats()["history"] == before+1  # Generated text is not delivered history.
    engine.acknowledge(result["reply_id"], "sent", "90001")
    assert engine.memory.stats()["history"] == before+2  # Only request copy was reduced.


@pytest.mark.asyncio
async def test_explicit_context_error_retries_once_with_smaller_prompt_and_own_trace(rig):
    engine, provider, _ = rig
    seed(engine)
    provider.errors = ["context_length_exceeded"]
    reply = await engine.handle(event())
    assert reply["mode"] == "text" and len(provider.calls) == 2 and used(engine) == 2
    first,second = (call[0] for call in provider.calls)
    assert estimate_tokens(second) < estimate_tokens(first)
    assert first[0] == second[0] and first[-1] == second[-1]
    rows = engine.trajectories.list()["items"]
    assert len(rows) == 2 and rows[0]["status"] == "success" and rows[1]["error_code"] == "context_length_exceeded"
    assert rows[0]["related_id"] == rows[1]["id"]
    assert [r["context_budget"]["attempt"] for r in rows] == [2,1]
    assert rows[0]["counts"]["dialogue"] == len(json.loads(second[1]["content"].split("\n",1)[1])["confirmed_direct_dialogue"])


@pytest.mark.asyncio
@pytest.mark.parametrize("code",["deepseek_http_400","deepseek_http_429","deepseek_http_503","generation_length_limit"])
async def test_other_provider_failures_never_trigger_compression_retry(rig,code):
    engine, provider, _ = rig
    seed(engine); provider.errors = [code]
    result = await engine.handle(event())
    assert result["mode"] == "none" and result["text"] == "" and len(provider.calls) == 1
    assert engine.trajectories.list()["items"][0]["error_code"] == code


@pytest.mark.asyncio
async def test_second_overflow_stops_without_third_request(rig):
    engine, provider, _ = rig
    seed(engine);provider.errors = ["context_length_exceeded"]*3
    result = await engine.handle(event())
    assert result["mode"] == "none" and len(provider.calls) == 2 and used(engine) == 2


@pytest.mark.asyncio
async def test_small_unreducible_prompt_is_never_retried_identically(rig):
    engine, provider, _ = rig
    provider.errors = ["context_length_exceeded"]
    assert (await engine.handle(event()))["mode"] == "none"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_retry_obeys_daily_quota_and_records_no_second_call(rig):
    engine, provider, settings = rig
    settings.daily_cloud_requests = 1;write_settings(engine.root,settings)
    seed(engine);provider.errors = ["context_length_exceeded"]
    result = await engine.handle(event())
    assert result["reason"] == "context_retry_budget" and len(provider.calls) == 1 and used(engine) == 1
    rows = engine.trajectories.list()["items"]
    assert rows[0]["status"] == "cloud_budget" and not rows[0]["provider_called"]


@pytest.mark.asyncio
async def test_oversized_core_stops_locally_without_daily_quota_or_silent_truncation(rig):
    engine, provider, _ = rig
    private_write(engine.content_root/"content/persona.md","不可截断核心设定"*5000)
    result = await engine.handle(event())
    assert result["reason"] == "context_budget_exceeded" and provider.calls == [] and used(engine) == 0
    row = engine.trajectories.list()["items"][0]
    assert row["context_budget"]["blocked"] and not row["provider_called"]
    assert row["error_code"] == "context_budget_exceeded"
    assert (engine.content_root/"content/persona.md").read_text() == "不可截断核心设定"*5000


@pytest.mark.asyncio
async def test_forget_during_first_failure_does_not_retry_or_restore_trace(rig):
    engine, provider, _ = rig
    seed(engine);provider.errors = ["context_length_exceeded"]
    provider.entered,provider.release = asyncio.Event(),asyncio.Event()
    task = asyncio.create_task(engine.handle(event()))
    await asyncio.wait_for(provider.entered.wait(),2)
    engine.forget_person(SCOPE,"20001")
    provider.release.set()
    result = await task
    assert result["mode"] == "none" and len(provider.calls) == 1
    assert engine.trajectories.list()["items"] == []


@pytest.mark.asyncio
async def test_delete_between_parent_lookup_and_child_creation_is_atomic(rig,monkeypatch):
    engine,provider,_ = rig
    seed(engine);provider.errors = ["context_length_exceeded"]
    exists = engine.trajectories.exists
    def delete_after_lookup(number):
        found = exists(number)
        engine.trajectories.delete(number)
        return found
    monkeypatch.setattr(engine.trajectories,"exists",delete_after_lookup)
    result = await engine.handle(event())
    assert result["mode"] == "none" and len(provider.calls) == 1
    assert engine.trajectories.list()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry",[False,True])
async def test_summary_only_advances_over_actually_sent_oldest_prefix(rig,retry):
    engine,provider,settings = rig
    ids = [engine.memory.add_history(SCOPE,"20001","user",f"声明{i}："+"甲"*1000,f"summary-{i}") for i in range(20)]
    if retry:
        provider.errors = ["context_length_exceeded"]
    result = await engine.summarize(SCOPE,"20001",provider,settings,force=True)
    sent = json.loads(provider.calls[-1][0][-1]["content"])
    assert 0 < len(sent) < len(ids)
    assert [row["id"] for row in sent] == ids[:len(sent)]
    assert result["processed"] == len(sent)
    state = engine.memory._db.execute("SELECT through_id FROM summary_progress").fetchone()
    assert state[0] == ids[len(sent)-1]
    next_claim = engine.memory.begin_summary(SCOPE,"20001",force=True)
    assert next_claim["batch"]["history"][0]["id"] == ids[len(sent)]
    traces = engine.trajectories.list()["items"]
    assert traces[0]["processed"] == len(sent) and traces[0]["counts"]["history"] == len(sent)
    if retry:
        assert traces[1]["status"] == "failed" and traces[1]["error_code"] == "context_length_exceeded"


@pytest.mark.asyncio
async def test_single_oversized_summary_source_never_skips_ahead(rig):
    engine,provider,settings = rig
    first = engine.memory.add_history(SCOPE,"20001","user","甲"*30000,"huge-source")
    engine.memory.add_history(SCOPE,"20001","user","很短的后续","short-source")
    result = await engine.summarize(SCOPE,"20001",provider,settings,force=True)
    assert provider.calls == [] and used(engine) == 0
    assert engine.memory._db.execute("SELECT through_id FROM summary_progress").fetchone()[0] < first
    assert engine.trajectories.list()["items"][0]["error_code"] == "context_budget_exceeded"


@pytest.mark.asyncio
async def test_privacy_erasure_during_local_fitting_never_calls_provider(rig,monkeypatch):
    from azusa_bot.context_budget import fit_context
    engine,provider,_ = rig
    seed(engine)
    def erased(*args,**kwargs):
        result = fit_context(*args,**kwargs)
        engine.forget_person(SCOPE,"20001")
        return result
    monkeypatch.setattr("azusa_bot.core.fit_context",erased)
    result = await engine.handle(event())
    assert result["mode"] == "none" and not provider.calls and used(engine) == 0
    assert engine.trajectories.list()["items"] == []
