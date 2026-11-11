"""Offline integration: one selected picture, revocation, ACK and profile safety."""
import asyncio
import base64
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from azusa_bot.assets import AssetStore
from azusa_bot.core import Incoming
from azusa_bot.providers import mock_group_plan
from azusa_bot.profiles import ProfileEngineManager
from azusa_bot.settings import Settings, private_write, write_settings
from context_helpers import context as wire_context

SCOPE = "onebot:10001:group:30001"


def upload(root, name="开心", fmt="PNG", **values):
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(out, format=fmt)
    return AssetStore(root).upload(dict(name=name, content_base64=base64.b64encode(out.getvalue()).decode(),
                                       keywords=["开心"], auto_send=True, **values))


def event(text="今天好开心", **values):
    return Incoming(**(dict(transport="onebot", app_id="10001", kind="dm", scope_id="20001",
                           user_id="20001", event_id="1", text=text) | values))


class Provider:
    def __init__(self, asset):
        self.calls = []
        self.plan_calls = []
        self.entered = self.release = None
        self.answer = dict(mode="image", asset_id=asset["id"], text="太开心了！", participate=True,
                           continuation=True, confidence=.95)

    async def complete(self, messages, **kwargs):
        data = wire_context(messages)
        if data.get('task') == 'group_plan':
            self.plan_calls.append(messages)
            planned = mock_group_plan(data)
            planned['confidence'] = self.answer['confidence']
            if self.answer.get('participate') is False:
                planned['participation_plan']['action'] = 'observe'
            return planned
        self.calls.append(messages)
        if self.entered:
            self.entered.set()
            await self.release.wait()
        return self.answer

    async def synthesize(self, *args):
        raise AssertionError("Images must never trigger TTS")


@pytest.fixture
def rig(tmp_path):
    private_write(tmp_path / "content/persona.md", "# 合成角色\n自然聊天。")
    write_settings(tmp_path, Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
        onebot_allowed_groups=["30001"], onebot_allowed_dm_users=["20001"], min_interval_seconds=0,
        group_chat_enabled=True, group_chat_debounce_seconds=0, group_chat_min_messages=1))
    manager = ProfileEngineManager(tmp_path)
    asset = upload(manager.content_root)
    provider = Provider(asset)
    manager.provider_override = provider
    yield manager, asset, provider
    manager.close()


@pytest.mark.asyncio
async def test_automatic_image_is_one_output_with_audited_candidates(rig):
    manager, asset, provider = rig
    scope = "onebot:10001:dm:20001"
    manager.memory.set_consent(scope, "20001", True)
    reply = await manager.handle(event())
    assert reply["mode"] == "image" and reply["text"] == ""
    assert Path(reply["media_path"]).is_file() and manager.authorize_reply(reply["reply_id"])
    assert "asset_reference" not in reply
    manager.acknowledge(reply["reply_id"], "sent", "90001")
    records = manager.memory.recent_history(scope, "20001")
    assert records[-1]["text"] == "[发送自定义表情：开心]"
    assert provider.answer["text"] not in [row["text"] for row in records]
    trace = manager.trajectories.list()["items"][0]
    detail = manager.trajectories.get(trace["id"], manager.content_root)
    assert detail["references"]["assets"][0]["id"] == asset["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["not_candidate", "explicit_text", "disabled", "not_relevant"])
async def test_model_cannot_choose_unoffered_or_unapproved_asset(rig, reason):
    manager, asset, provider = rig
    text = "今天好开心"
    if reason == "not_candidate":
        provider.answer["asset_id"] = "file:///etc/passwd"
    elif reason == "explicit_text":
        text = "/文字 今天好开心"
    elif reason == "disabled":
        AssetStore(manager.content_root).update(asset["id"], {"auto_send": False})
    else:
        text = "请解释相对论"
    result = await manager.handle(event(text))
    assert result["mode"] == "text" and result["text"] == provider.answer["text"]
    assert not result.get("media_path")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["approved", "automatic", "delete", "bytes", "profile"])
async def test_prepared_image_revoked_before_send(rig, change):
    manager, asset, provider = rig
    result = await manager.handle(event())
    assert manager.authorize_reply(result["reply_id"])
    store = AssetStore(manager.content_root)
    if change == "approved":
        store.update(asset["id"], {"approved": False})
    elif change == "automatic":
        store.update(asset["id"], {"auto_send": False})
    elif change == "delete":
        store.delete(asset["id"])
    elif change == "bytes":
        manifest = json.loads((store.base / "manifest.json").read_text())
        Image.new("RGB", (8, 8), "blue").save(store.base / manifest["开心"]["file"])
    else:
        role = manager.profiles.create({"name": "其他角色"})
        manager.activate(role["id"])
    assert not manager.authorize_reply(result["reply_id"])


@pytest.mark.asyncio
async def test_edit_while_model_waits_does_not_select_new_revision(rig):
    manager, asset, provider = rig
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(manager.handle(event()))
    await asyncio.wait_for(provider.entered.wait(), 2)
    AssetStore(manager.content_root).update(asset["id"], {"emotion": "另一种表情含义"})
    provider.release.set()
    result = await task
    assert result["mode"] == "text"
    assert not list((manager.root / "data/media").glob("*.png"))


@pytest.mark.asyncio
async def test_group_image_ack_uses_actual_image_and_keeps_speaking_budget(rig):
    manager, asset, provider = rig
    incoming = event(kind="group", scope_id="30001", unprompted=True)
    result = await manager.handle(incoming)
    assert result["mode"] == "image" and result["text"] == ""
    assert not any(row["role"] == "assistant" for row in manager.groups.snapshot(SCOPE))
    manager.acknowledge(result["reply_id"], "sent", "90001")
    assert manager.groups.snapshot(SCOPE)[-1]["text"] == "[发送自定义表情：开心]"
    assert manager._group_threads[(SCOPE, "20001")]["messages"][-1]["text"] == "[发送自定义表情：开心]"
    later = await manager.handle(event(kind="group", scope_id="30001", unprompted=True,
                                       event_id="2", user_id="20002"))
    assert later["mode"] == "none" and len(provider.calls) == 1


@pytest.mark.asyncio
async def test_manual_selection_does_not_need_model_or_automatic_opt_in(rig):
    manager, asset, provider = rig
    AssetStore(manager.content_root).update(asset["id"], {"auto_send": False})
    result = await manager.handle(event("/表情 开心"))
    assert result["mode"] == "image" and manager.authorize_reply(result["reply_id"])
    assert provider.calls == []


def test_cancelled_webp_is_removed(rig):
    manager, _, _ = rig
    item = upload(manager.content_root, "WebP测试", "WEBP")
    result = manager.asset(item["id"])
    path = Path(result["media_path"])
    manager.discard_generated_media(result)
    assert not path.exists()


@pytest.mark.asyncio
async def test_image_followup_uses_same_candidates_and_single_output(rig):
    manager, asset, provider = rig
    incoming = event(kind="group", scope_id="30001", mentioned=True)
    first = await manager.handle(incoming)
    manager.acknowledge(first["reply_id"], "sent", "90010")
    second = await manager.handle(event("确实很开心", kind="group", scope_id="30001",
                                         unprompted=True, event_id="next"))
    assert second["mode"] == "image" and second["text"] == ""
    with manager.ledger.db() as db:
        state = dict(db.execute("SELECT * FROM deliveries WHERE reply_id=?",(second["reply_id"],)).fetchone())
    assert manager.authorize_reply(second["reply_id"]), state


@pytest.mark.asyncio
async def test_image_never_bypasses_group_silence_decision(rig):
    manager, _, provider = rig
    provider.answer["participate"] = False
    reply = await manager.handle(event(kind="group",scope_id="30001",unprompted=True))
    assert reply["mode"] == "none" and reply["reason"] == "conversation_observed"
    assert len(provider.plan_calls) == 1 and not provider.calls
    assert not list((manager.root / "data/media").glob("*.png"))


@pytest.mark.asyncio
async def test_deleted_asset_name_not_resolved_from_old_trace(rig):
    manager, asset, _ = rig
    await manager.handle(event())
    trace = manager.trajectories.list()["items"][0]
    AssetStore(manager.content_root).delete(asset["id"])
    detail = manager.trajectories.get(trace["id"],manager.content_root)
    reference = detail["references"]["assets"][0]
    assert reference["available"] is False and "name" not in reference
