"""Provider audit references must explain context without creating another memory archive."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from context_helpers import context as wire_context
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from azusa_bot import lore
from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import ProviderError, mock_group_plan
from azusa_bot.settings import Settings, write_settings
from azusa_bot.trajectories import references
from azusa_bot.trajectory_admin import create_router

SCOPE = "onebot:10001:dm:20001"


class Provider:
    def __init__(self):
        self.calls = []
        self.plan_calls = []
        self.error = None
        self.started = None
        self.release = None
        self.summary_result = None
        self.reply_result = None

    async def complete(self, messages, *, summary=False):
        data = wire_context(messages)
        if not summary and data.get('task') == 'group_plan':
            self.plan_calls.append(messages)
            answer = mock_group_plan(data)
            if self.reply_result and self.reply_result.get('participate') is False:
                answer['participation_plan']['action'] = 'observe'
            return answer
        self.calls.append((messages, summary))
        if self.started:
            self.started.set()
            await self.release.wait()
        if self.error:
            raise self.error
        if summary:
            rows = json.loads(messages[-1]["content"])
            return self.summary_result if self.summary_result is not None else {
                "memories": [{"text": "用户自述喜欢合成测试猫", "source_ids": [rows[0]["id"]]}]}
        return self.reply_result or {"text": "合成测试回答", "mode": "text"}


@pytest.fixture
def rig(tmp_path):
    (tmp_path/"content").mkdir()
    (tmp_path/"content/persona.md").write_text("合成测试角色")
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
                        onebot_allowed_dm_users=["20001", "20002"], min_interval_seconds=0,
                        auto_summary_every=12, read_existing_dsh=False)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    provider = Provider()
    engine.provider_override = provider
    yield engine, provider, settings
    engine.memory.close()
    engine.groups.close()


def event(number=1, user="20001"):
    return Incoming(transport="onebot", app_id="10001", kind="dm", scope_id=user,
                    user_id=user, event_id=str(number), text="合成测试猫")


def knowledge(engine):
    result = lore.import_documents(engine.content_root, [{"name": "test-cats.md", "title": "合成测试猫资料",
        "category": "reference", "sources": ["https://example.org/cats"], "tags": ["合成测试猫"],
        "text": "合成测试猫的公开背景资料，完全虚构，仅供自动测试。"}])
    return result["documents"][0]["id"]


@pytest.mark.asyncio
async def test_exact_injected_references_and_no_second_prompt_archive(rig):
    engine, provider, _ = rig
    engine.memory.set_consent(SCOPE, "20001", True)
    number = engine.memory.add_memory(SCOPE, "20001", "本人明确说喜欢合成测试猫")
    history_id = engine.memory.add_history(SCOPE, "20001", "user", "之前提到合成测试猫", "earlier")
    doc_id = knowledge(engine)
    result = await engine.handle(event())
    assert result["mode"] == "text"
    row = engine.trajectories.list()["items"][0]
    assert row["kind"] == "reply" and row["provider_called"] and row["status"] == "success"
    assert row["source_event_id"] == "1"
    assert row["started_at"] <= row["finished_at"]
    prompt = json.loads(provider.calls[0][0][1]["content"].split("\n", 1)[1])
    detail = engine.trajectories.get(row["id"], engine.content_root)
    assert [r["id"] for r in detail["references"]["memories"]] == [r["id"] for r in prompt["retrieved_memory"]] == [number]
    assert detail["references"]["history"] == []  # Raw archive is not current conversation.
    assert "之前提到合成测试猫" not in str(provider.calls[0][0])
    assert detail["references"]["knowledge"][0]["doc_id"] == doc_id
    assert all(r["available"] for group in ("memories", "knowledge", "history") for r in detail["references"][group])
    assert detail["references"]["persona"]["available"]
    stored = str(engine.memory._db.execute("SELECT refs,result FROM trajectories").fetchall()[0][:])
    assert "喜欢合成测试猫" not in stored
    assert row["request_tail"] == "合成测试猫" and row["response_text"] == "合成测试回答"
    assert "公开背景资料" not in stored and "之前提到" not in stored


@pytest.mark.asyncio
async def test_source_correction_and_deletion_never_show_old_or_new_text_as_old_version(rig):
    engine, _, _ = rig
    engine.memory.set_consent(SCOPE, "20001", True)
    number = engine.memory.add_memory(SCOPE, "20001", "喜欢合成测试猫旧事实")
    history_id = engine.memory.add_history(SCOPE, "20001", "user", "合成测试猫旧原文", "earlier")
    doc_id = knowledge(engine)
    await engine.handle(event())
    trace = engine.trajectories.list()["items"][0]["id"]
    engine.memory.update_memory(number, SCOPE, "20001", "更正后的新事实")
    engine.memory.update_history(history_id, SCOPE, "20001", "更正后的新原文")
    lore.update_document(engine.content_root, doc_id, {"text": "更正后的背景"})
    detail = engine.trajectories.get(trace, engine.content_root)
    assert not any(r["available"] for group in ("memories", "history", "knowledge") for r in detail["references"][group])
    assert "旧事实" not in str(detail) and "新事实" not in str(detail) and "新原文" not in str(detail)
    engine.memory.delete_memory(number, SCOPE, "20001")
    assert not engine.trajectories.get(trace, engine.content_root)["references"]["memories"][0]["available"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["forget", "disable"])
async def test_withdrawal_while_provider_runs_cannot_recreate_trace(rig, operation):
    engine, provider, _ = rig
    engine.memory.set_consent(SCOPE, "20001", True)
    provider.started, provider.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(engine.handle(event()))
    await provider.started.wait()
    assert engine.trajectories.list()["items"][0]["status"] == "running"
    if operation == "forget":
        engine.forget_person(SCOPE, "20001")
    else:
        engine.memory.set_consent(SCOPE, "20001", False)
        engine.clear_group_context(SCOPE)
    provider.release.set()
    await task
    assert engine.trajectories.list()["items"] == []


@pytest.mark.asyncio
async def test_summary_timestamps_trigger_sources_and_actual_created_ids(rig):
    engine, provider, settings = rig
    engine.memory.set_consent(SCOPE, "20001", True)
    for n in range(12):
        engine.memory.add_history(SCOPE, "20001", "user", f"合成测试猫偏好 {n}", str(n))
    result = await engine.summarize_due()
    trace = engine.trajectories.list(kind="summary")["items"][0]
    assert trace["trigger"] == "background" and trace["status"] == "saved"
    assert trace["processed"] == 12 and trace["saved"] == 1
    assert trace["output_memory_ids"] == result["memory_ids"]
    assert trace["counts"]["history"] == 12
    provider.summary_result = {"memories": []}
    engine.memory.add_history(SCOPE, "20001", "user", "没有长期价值的新测试消息", "new")
    await engine.summarize(SCOPE, "20001", provider, settings, force=True)
    rows = engine.trajectories.list(kind="summary")["items"]
    assert rows[0]["trigger"] == "manual" and rows[0]["status"] == "no_facts"
    assert rows[0]["processed"] == 1 and rows[0]["saved"] == 0 and len(rows) == 2


@pytest.mark.asyncio
async def test_failure_is_allowlisted_and_preparation_failure_is_not_a_provider_call(rig):
    engine, provider, settings = rig
    provider.error = ProviderError("SECRET response body with token=private-secret")
    await engine.handle(event())
    row = engine.trajectories.list()["items"][0]
    assert row["error_code"] == "provider_unknown_error" and row["status"] == "failed"
    assert "SECRET" not in str(row) and "private-secret" not in str(row)
    engine.memory.set_consent(SCOPE, "20001", True)
    engine.memory.add_history(SCOPE, "20001", "user", "合成测试猫", "summary")
    def unavailable(_):
        raise ProviderError("credential_store_unavailable")
    engine.providers = unavailable
    await engine.summarize(SCOPE, "20001", None, settings, force=True)
    row = engine.trajectories.list(kind="summary")["items"][0]
    assert row["status"] == "failed" and not row["provider_called"]
    assert row["error_code"] == "credential_store_unavailable"


@pytest.mark.asyncio
async def test_cancelled_provider_call_is_visible_as_interrupted(rig):
    engine, provider, _ = rig
    provider.started, provider.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(engine.handle(event()))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.trajectories.list()["items"][0]["status"] == "interrupted"


def test_retention_and_pagination_are_bounded(rig):
    engine, _, _ = rig
    store = engine.trajectories
    store.MAX_RECORDS = 3
    for _ in range(5):
        n = store.begin("reply", "direct", SCOPE, "20001", "mock", "mock", references())
        store.finish(n)
    rows = store.list(limit=2)
    assert len(rows["items"]) == 2 and len(store.list(before=rows["next_before"])["items"]) == 1
    with engine.memory._db:
        engine.memory._db.execute("UPDATE trajectories SET started_at=?", (time.time()-31*86400,))
    assert store.list()["items"] == []


@pytest.mark.asyncio
async def test_parallel_members_do_not_mix_references_and_profiles_have_separate_tables(rig, tmp_path):
    engine, _, settings = rig
    for user in ("20001", "20002"):
        engine.memory.add_memory(f"onebot:10001:dm:{user}", user, "合成测试猫属于 "+user)
    await asyncio.gather(engine.handle(event(1)), engine.handle(event(2, "20002")))
    for row in engine.trajectories.list()["items"]:
        own = engine.trajectories.get(row["id"], engine.content_root)["references"]["memories"][0]["preview"]
        assert row["user"] in own
    profile = tmp_path/"data/profiles/alternate"
    profile.mkdir(parents=True)
    other = Engine(tmp_path, profile_root=profile, profile_id="alternate")
    try:
        assert other.trajectories.list()["items"] == []
    finally:
        other.memory.close(); other.groups.close()


@pytest.mark.asyncio
async def test_source_change_during_detail_resolution_does_not_return_cached_preview(rig, monkeypatch):
    engine, _, _ = rig
    number = engine.memory.add_memory(SCOPE, "20001", "合成测试猫旧版本")
    knowledge(engine)
    await engine.handle(event())
    trace = engine.trajectories.list()["items"][0]["id"]
    original = lore.get_document
    def changed(root, doc):
        engine.memory.update_memory(number, SCOPE, "20001", "新版本")
        return original(root, doc)
    monkeypatch.setattr(lore, "get_document", changed)
    assert engine.trajectories.get(trace, engine.content_root)["references"]["memories"] == [{"id": number, "available": False}]


@pytest.mark.asyncio
async def test_admin_router_is_local_authenticated_and_profile_scoped(rig):
    engine, _, _ = rig
    await engine.handle(event())
    manager = SimpleNamespace(current=engine)
    app = FastAPI()
    def auth(request: Request):
        if request.headers.get("Authorization") != "Bearer synthetic-test-admin":
            raise HTTPException(401)
    app.include_router(create_router(manager), dependencies=[Depends(auth)])
    with TestClient(app) as client:
        assert client.get("/admin/trajectories").status_code == 401
        headers = {"Authorization": "Bearer synthetic-test-admin"}
        listing = client.get("/admin/trajectories", headers=headers).json()
        number = listing["items"][0]["id"]
        assert listing["profile_id"] == "default"
        assert listing["items"][0]["delivery"]["state"] == "unconfirmed"
        assert "delivery_event_key" not in listing["items"][0]
        assert client.get(f"/admin/trajectories/{number}", headers=headers).json()["references"]
        assert client.get("/admin/trajectories?kind=unknown", headers=headers).status_code == 400
        assert client.delete(f"/admin/trajectories/{number}", headers=headers).json()["deleted"]
        assert client.get(f"/admin/trajectories/{number}", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_group_decision_keeps_exact_retrieval_but_not_other_members_transcript(rig):
    engine, provider, settings = rig
    settings.group_chat_enabled = True
    settings.onebot_allowed_groups = ["30001"]
    settings.group_chat_debounce_seconds = .01
    settings.group_chat_min_messages = 1
    write_settings(engine.root, settings)
    scope = "onebot:10001:group:30001"
    number = engine.memory.add_memory(scope, "20001", "喜欢合成测试猫")
    engine.groups.add(scope, "20002", "其他成员未同意保存的独特群聊正文", "earlier")
    provider.reply_result = {"text": "合成测试猫的话题可以接着聊。", "mode": "text"}
    incoming = event().model_copy(update={"kind": "group", "scope_id": "30001", "unprompted": True})
    result = await engine.handle(incoming)
    assert result["mode"] == "text"
    row = engine.trajectories.list()["items"][0]
    assert row["kind"] == "group_decision" and row["participation_plan"]["action"] == "reply"
    assert row["counts"]["group_context"] == 2
    detail = engine.trajectories.get(row["id"], engine.content_root)
    assert detail["references"]["memories"][0]["id"] == number
    assert "独特群聊正文" not in str(detail)
    assert "独特群聊正文" not in engine.memory._db.execute("SELECT refs FROM trajectories").fetchone()[0]
    planning = engine.trajectories.list(kind="group_plan")["items"]
    assert len(planning) == 1 and planning[0]["counts"]["memories"] == 0
    assert "喜欢合成测试猫" not in str(provider.plan_calls)


@pytest.mark.asyncio
async def test_followup_trajectory_counts_confirmed_dialogue(rig):
    engine, provider, settings = rig
    settings.group_chat_enabled = True
    settings.onebot_allowed_groups = ["30001"]
    settings.group_chat_debounce_seconds = .01
    settings.group_followup_min_interval_seconds = 2
    write_settings(engine.root, settings)
    direct = event().model_copy(update={"kind": "group", "scope_id": "30001", "mentioned": True})
    result = await engine.handle(direct)
    engine.acknowledge(result["reply_id"], "sent", "55555")
    provider.reply_result = {"participate": True, "continuation": True, "confidence": .99, "text": "继续这个话题，换一条全新合成回复。"}
    followup = direct.model_copy(update={"event_id": "later", "text": "那然后呢？", "mentioned": False, "unprompted": True})
    await engine.handle(followup)
    rows = engine.trajectories.list(kind="followup")["items"]
    assert len(rows) == 1 and rows[0]["counts"]["dialogue"] == 2
    assert rows[0]["continuation"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_voice_call_is_related_to_reply_and_labels_bounded_spoken_preview(rig, failed):
    engine, provider, settings = rig
    settings.voice_enabled = True
    settings.voice_authorized = True
    write_settings(engine.root, settings)
    provider.reply_result = {"text": "唯一的合成语音正文", "mode": "voice"}
    async def synthesize(text):
        if failed:
            raise ProviderError("fish_http_503")
        return engine.root/"data/media/synthetic.wav"
    provider.synthesize = synthesize
    result = await engine.handle(event())
    assert result["mode"] == ("text" if failed else "voice")
    rows = engine.trajectories.list()["items"]
    assert [r["kind"] for r in rows] == ["tts", "reply"]
    assert rows[0]["related_id"] == rows[1]["id"]
    assert rows[0]["status"] == ("failed" if failed else "success")
    detail = engine.trajectories.get(rows[0]["id"], engine.content_root)
    assert detail["request_tail"] == "唯一的合成语音正文" and detail["request_kind"] == "speech"
    assert detail["response_text"] == (None if failed else "唯一的合成语音正文")
