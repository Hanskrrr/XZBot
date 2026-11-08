"""No-mention group speech reaches the same single-output TTS boundary.

All identities, persona, model decisions and PCM audio are synthetic. Nothing
loads production content/credentials or contacts a cloud service/QQ account.
"""
import asyncio
import json
from pathlib import Path
import time
import uuid
import wave

import pytest

from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import ProviderError
from azusa_bot.settings import Settings, private_write, write_settings


SCOPE = "onebot:10001:group:30001"


class Provider:
    fish_model = "synthetic-tts"

    def __init__(self, root):
        self.root = root
        self.calls, self.speech = [], []
        self.answer = {"participate": True, "continuation": True, "confidence": .95,
                       "mode": "voice", "text": "在呢，今天也要早点休息。"}
        self.error = None
        self.entered = self.release = None
        self.generated = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return {"memories": []} if kwargs.get("summary") else dict(self.answer)

    async def synthesize(self, text):
        self.speech.append(text)
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        if self.error:
            raise ProviderError(self.error)
        target = self.root / "data/media" / (uuid.uuid4().hex + ".wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000)
            output.writeframes(b"\x00\x00" * 240)
        self.generated.append(target)
        return target


@pytest.fixture
def rig(tmp_path):
    private_write(tmp_path / "content/persona.md", "# 合成角色\n短句自然回应，不宣称已发送语音。")
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
        onebot_allowed_groups=["30001"], group_chat_enabled=True,
        group_chat_min_messages=3, group_chat_debounce_seconds=0,
        min_interval_seconds=0, group_followup_min_interval_seconds=2,
        voice_enabled=True, voice_authorized=True, fish_reference_id="synthetic-voice",
        fish_model="synthetic-tts", fish_model_override=True)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    provider = Provider(tmp_path)
    engine.provider_override = provider
    engine.address_names_loader = lambda: ["阿梓"]
    yield engine, provider, settings
    engine.vision.clear(); engine.memory.close(); engine.groups.close()


def event(text="阿梓，发语音说声晚安", **changes):
    return Incoming(**(dict(transport="onebot", app_id="10001", kind="group", scope_id="30001",
        user_id="20001", event_id="1", text=text, mentioned=False, unprompted=True) | changes))


def traces(engine, kind):
    return [row for row in engine.trajectories.list()["items"] if row["kind"] == kind]


@pytest.mark.asyncio
async def test_named_without_mention_synthesizes_once_and_audits_origin(rig):
    engine, provider, _ = rig
    reply = await engine.handle(event())
    assert reply["mode"] == "voice"
    assert provider.speech == [provider.answer["text"]]
    assert Path(reply["media_path"]).is_file()
    assert "语音已发送" not in reply["text"]
    tts, decision = traces(engine, "tts"), traces(engine, "group_decision")
    assert len(tts) == len(decision) == 1
    assert tts[0]["scope"] == SCOPE and tts[0]["user"] == "20001"
    assert tts[0]["related_id"] == decision[0]["id"]
    assert tts[0]["trigger"] == "group_observation"
    assert tts[0]["provider_called"] and tts[0]["status"] == "success"
    # Preparing speech does not invent a send or prematurely open a dialogue.
    assert not engine._group_threads
    assert not any(row["role"] == "assistant" for row in engine.groups.snapshot(SCOPE))


@pytest.mark.asyncio
async def test_confirmed_followup_without_mention_can_switch_from_text_to_voice(rig):
    engine, provider, _ = rig
    provider.answer.update(mode="text", text="晚安之前，记得把手机放下休息一会儿。")
    first = await engine.handle(event("阿梓，晚上好"))
    assert first["mode"] == "text" and provider.speech == []
    engine.acknowledge(first["reply_id"], "sent", "90001")
    with engine.groups.db() as db:
        db.execute("UPDATE group_activity SET at=?", (time.time()-10,))
        db.execute("UPDATE group_replies SET at=?", (time.time()-10,))
    provider.answer.update(mode="voice", text="好呀，祝你今晚做个好梦。")
    reply = await engine.handle(event("能用语音再说一句吗", event_id="2"))
    assert reply["mode"] == "voice" and provider.speech == [provider.answer["text"]]
    tts = traces(engine, "tts")
    followup = traces(engine, "followup")
    assert len(tts) == len(followup) == 1
    assert tts[0]["trigger"] == "followup" and tts[0]["related_id"] == followup[0]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["silence", "low_confidence", "text", "voice_disabled", "not_authorized"])
async def test_voice_is_not_synthesized_without_participation_mode_and_permission(rig, choice):
    engine, provider, settings = rig
    if choice == "silence": provider.answer["participate"] = False
    elif choice == "low_confidence": provider.answer["confidence"] = .2
    elif choice == "text": provider.answer["mode"] = "text"
    elif choice == "voice_disabled": settings.voice_enabled = False
    elif choice == "not_authorized": settings.voice_authorized = False
    write_settings(engine.root, settings)
    reply = await engine.handle(event())
    assert provider.speech == []
    assert reply["mode"] == ("none" if choice in {"silence", "low_confidence"} else "text")
    assert traces(engine, "tts") == []


@pytest.mark.asyncio
async def test_tts_failure_returns_one_actual_text_without_placeholder(rig):
    engine, provider, _ = rig
    provider.answer["text"] = "晚安，明天见。（语音已发送。）"
    provider.error = "fish_http_503"
    reply = await engine.handle(event())
    assert reply["mode"] == "text" and reply["text"] == "晚安，明天见。"
    assert provider.speech == ["晚安，明天见。"]
    assert not reply.get("media_path")
    assert len(traces(engine, "tts")) == 1 and traces(engine, "tts")[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_model_can_reply_when_no_cloud_quota_remains_for_speech(rig):
    engine, provider, settings = rig
    settings.provider_mode = "cloud"
    settings.daily_cloud_requests = 1
    write_settings(engine.root, settings)
    reply = await engine.handle(event())
    assert len(provider.calls) == 1 and provider.speech == []
    assert reply["mode"] == "text" and reply["text"] == provider.answer["text"]
    assert not reply.get("media_path")
    tts = traces(engine, "tts")
    assert len(tts) == 1 and not tts[0]["provider_called"]
    assert tts[0]["error_code"] == "voice_budget_exhausted"
    with engine.ledger.db() as db:
        assert db.execute("SELECT SUM(used) FROM budget").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["安" * 181, "去这个链接看看：https://example.com", "示例代码：```print(1)```"])
async def test_voice_selection_keeps_long_text_links_and_code_as_text(rig, text):
    engine, provider, _ = rig
    provider.answer["text"] = text
    reply = await engine.handle(event())
    assert reply["mode"] == "text" and reply["text"] == text
    assert provider.speech == [] and traces(engine, "tts") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["forget", "profile", "power", "voice_disabled"])
async def test_tts_inflight_revocation_cannot_send_old_media(rig, change):
    engine, provider, settings = rig
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(engine.handle(event()))
    try:
        await asyncio.wait_for(provider.entered.wait(), 1)
        if change == "forget":
            forgotten = await engine.handle(event("/忘记我", event_id="2"))
            assert "已删除" in forgotten["text"]
        elif change == "profile":
            engine._active_guard = lambda: False
            engine.invalidate_groups()
        elif change == "power":
            private_write(engine.root / "data/bot-control.json", json.dumps({
                "schema": 1, "state": "stopped", "epoch": uuid.uuid4().hex,
                "updated_at": time.time(), "error_code": ""}))
            engine.invalidate_groups()
        else:
            settings.voice_enabled = False; write_settings(engine.root, settings)
            engine.invalidate_groups()
    finally:
        provider.release.set()
        result = await task
    assert result["mode"] == "none" and not result.get("media_path")
    assert len(provider.speech) == 1 and all(not p.exists() for p in provider.generated)
