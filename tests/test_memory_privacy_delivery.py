"""A local privacy edit revokes generated and prepared direct-message replies."""
import asyncio

import pytest

from azusa_bot.core import Incoming
from azusa_bot.profiles import ProfileEngineManager
from azusa_bot.settings import Settings, write_settings


SCOPE = "onebot:10001:dm:20001"


class PausedProvider:
    def __init__(self, root, stage):
        self.root, self.stage = root, stage
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.path = root / "data/media" / ("a" * 32 + ".wav")

    async def complete(self, messages, *, summary=False):
        assert not summary
        if self.stage == "text":
            self.entered.set()
            await self.release.wait()
        return {"mode": "voice" if self.stage == "voice" else "text", "text": "这句引用了旧的合成偏好。"}

    async def synthesize(self, text):
        self.entered.set()
        await self.release.wait()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"synthetic old context audio")
        return self.path


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "content").mkdir()
    (tmp_path / "content/persona.md").write_text("合成角色，测试隐私撤销。")
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
        onebot_allowed_dm_users=["20001"], min_interval_seconds=0,
        voice_enabled=True, voice_authorized=True, fish_reference_id="synthetic_voice")
    write_settings(tmp_path, settings)
    engine = ProfileEngineManager(tmp_path)
    engine.memory.set_consent(SCOPE, "20001", True)
    engine.memory.add_memory(SCOPE, "20001", "旧的合成偏好")
    yield engine, tmp_path
    engine.close()


def event():
    return Incoming(transport="onebot", app_id="10001", kind="dm", scope_id="20001",
                    user_id="20001", event_id="private-inflight", text="我以前喜欢什么？")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["text", "voice"])
@pytest.mark.parametrize("action", ["disable", "delete", "forget"])
async def test_privacy_revocation_during_generation_silences_dm_and_removes_new_audio(runtime, stage, action):
    engine, root = runtime
    provider = PausedProvider(root, stage)
    engine.provider_override = provider
    task = asyncio.create_task(engine.handle(event()))
    await asyncio.wait_for(provider.entered.wait(), 2)
    if action == "disable":
        engine.memory.set_consent(SCOPE, "20001", False)
        engine.clear_group_context(SCOPE)
    elif action == "delete":
        fact = engine.memory.list_memories(SCOPE, "20001")[0]
        engine.memory.delete_memory(fact["id"], SCOPE, "20001")
        engine.clear_group_context(SCOPE)
    else:
        engine.forget_person(SCOPE, "20001")
    provider.release.set()
    reply = await asyncio.wait_for(task, 2)
    assert reply["mode"] == "none"
    assert not engine.authorize_reply(reply["reply_id"])
    assert not provider.path.exists()


@pytest.mark.asyncio
async def test_privacy_edit_revokes_prepared_reply_before_bridge_send(runtime):
    engine, root = runtime
    provider = PausedProvider(root, "text")
    provider.release.set()
    engine.provider_override = provider
    reply = await engine.handle(event())
    assert reply["mode"] == "text" and engine.authorize_reply(reply["reply_id"])
    engine.forget_person(SCOPE, "20001")
    assert not engine.authorize_reply(reply["reply_id"])


@pytest.mark.asyncio
async def test_profile_switch_during_tts_removes_discarded_audio(runtime):
    engine, root = runtime
    provider = PausedProvider(root, "voice")
    engine.provider_override = provider
    task = asyncio.create_task(engine.handle(event()))
    await asyncio.wait_for(provider.entered.wait(), 2)
    role = engine.profiles.create({"name": "另一个合成角色"})
    engine.activate(role["id"])
    provider.release.set()
    reply = await asyncio.wait_for(task, 2)
    assert reply["mode"] == "none"
    assert not engine.authorize_reply(reply["reply_id"])
    assert not provider.path.exists()
