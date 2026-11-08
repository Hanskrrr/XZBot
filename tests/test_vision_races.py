"""Concurrency and scope checks with generated pixels and a synthetic persona."""
import asyncio
import io
import json

import httpx
from PIL import Image
import pytest
from context_helpers import context as wire_context

from azusa_bot.prompts import DEFAULTS
from azusa_bot.core import Engine, Incoming
from azusa_bot.settings import Settings, private_write, write_settings
from azusa_bot.vision import VisionCache


SCOPE = "onebot:10001:group:30001"
URL = "https://gchat.qpic.cn/synthetic-race.png"


def pixels():
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(output, format="PNG")
    return output.getvalue()


class Provider:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_first = False

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if self.block_first and len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return {"text": "可以一起看看这个话题。", "mode": "text", "participate": True,
                "continuation": True, "confidence": .99}


@pytest.fixture
def rig(tmp_path):
    private_write(tmp_path / "content/persona.md", "# 合成角色\n根据真正提供的内容回答，不编造。")
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
        onebot_allowed_groups=["30001"], group_chat_enabled=True,
        group_chat_debounce_seconds=0, group_chat_min_messages=1,
        group_chat_decision_interval_seconds=30, group_followup_enabled=False,
        group_followup_min_interval_seconds=2, min_interval_seconds=0)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    provider = Provider()
    engine.provider_override = provider
    engine.address_names_loader = lambda: ["合成角色"]
    engine.vision = VisionCache(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=pixels())))
    yield engine, provider, settings
    engine.vision.clear()
    engine.groups.close()
    engine.memory.close()


def event(**updates):
    return Incoming(**(dict(transport="onebot", app_id="10001", kind="group", scope_id="30001",
        user_id="20001", event_id="1", text="大家有什么建议", unprompted=True) | updates))


def image_parts(messages):
    return [part for message in messages if isinstance(message["content"], list)
            for part in message["content"] if part["type"] == "image_url"]


@pytest.mark.asyncio
async def test_named_reply_survives_new_ordinary_message_without_losing_target(rig):
    engine, provider, _ = rig
    provider.block_first = True
    first = asyncio.create_task(engine.handle(event(text="合成角色，你有什么建议")))
    await asyncio.wait_for(provider.entered.wait(), 1)
    second = asyncio.create_task(engine.handle(event(event_id="2", user_id="20002", text="我也在这里")))
    await asyncio.sleep(0)
    assert [row["message_id"] for row in engine.groups.snapshot(SCOPE)] == ["1", "2"]
    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result["mode"] == "text"
    assert second_result["reason"] == "decision_cooldown"
    assert len(provider.calls) == 1
    context = wire_context(provider.calls[0])
    assert context["current_message"]["user_id"] == "20001"
    assert context["current_message"]["message_id"] == "1"


@pytest.mark.asyncio
async def test_privacy_withdrawal_preempts_named_reply_in_progress(rig):
    engine, provider, _ = rig
    provider.block_first = True
    first = asyncio.create_task(engine.handle(event(text="合成角色，你怎么看", images=[URL])))
    await asyncio.wait_for(provider.entered.wait(), 1)
    assert image_parts(provider.calls[0])
    forgotten = await asyncio.wait_for(engine.handle(event(event_id="2", text="/忘记我")), .5)
    assert "已删除" in forgotten["text"]
    provider.release.set()
    result = await first
    assert result["mode"] == "none" and result["reason"] == "context_changed_during_generation"
    assert engine.vision.select(SCOPE, ["1"]) == []
    assert engine.trajectories.list(user="20001")["items"] == []


@pytest.mark.asyncio
async def test_busy_group_preserves_context_without_bypassing_paid_decision_interval(rig):
    engine, provider, _ = rig
    provider.block_first = True
    first = asyncio.create_task(engine.handle(event(text="大家讨论一下第一件事")))
    await asyncio.wait_for(provider.entered.wait(), 1)
    second = asyncio.create_task(engine.handle(event(event_id="2", user_id="20002", text="又补充了新的信息")))
    await asyncio.sleep(0)
    provider.release.set()
    old, fresh = await asyncio.gather(first, second)
    assert old["reason"] == "context_changed_during_generation" and old["mode"] == "none"
    assert fresh["reason"] == "decision_cooldown" and len(provider.calls) == 1
    assert [row["message_id"] for row in engine.groups.snapshot(SCOPE)] == ["1", "2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["forget", "disable_vision"])
async def test_privacy_or_closed_vision_during_download_prevents_cloud_submission(rig, action):
    engine, provider, settings = rig
    entered, release = asyncio.Event(), asyncio.Event()
    async def download(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=pixels())
    engine.vision = VisionCache(transport=httpx.MockTransport(download))
    first = asyncio.create_task(engine.handle(event(text="", images=[URL])))
    await asyncio.wait_for(entered.wait(), 1)
    if action == "forget":
        forgotten = await asyncio.wait_for(engine.handle(event(event_id="2", text="/忘记我")), .5)
        assert "已删除" in forgotten["text"]
    else:
        # The providers API persists its change and invalidates the engine.
        settings.vision_enabled = False
        write_settings(engine.root, settings)
        engine.invalidate_groups()
    release.set()
    result = await first
    assert result["mode"] == "none" and result["reason"] == "privacy_context_changed"
    assert provider.calls == [] and engine.vision.select(SCOPE, ["1"]) == []


@pytest.mark.asyncio
async def test_same_group_other_member_can_reference_picture_with_original_sender_label(rig):
    engine, provider, settings = rig
    settings.group_chat_min_messages = 3
    write_settings(engine.root, settings)
    first = await engine.handle(event(text="", images=[URL]))
    assert first["reason"] == "collecting_context" and provider.calls == []
    second = await engine.handle(event(event_id="2", user_id="20002", text="你看看刚刚那张图", mentioned=True))
    assert second["mode"] == "text" and len(image_parts(provider.calls[0])) == 1
    context = wire_context(provider.calls[0])
    assert context["trusted_identity"]["user_id"] == "20002"
    labels = [part["text"] for message in provider.calls[0] if isinstance(message["content"], list)
              for part in message["content"] if part["type"] == "text" and part["text"].startswith(DEFAULTS["image_label"])]
    source = json.loads(labels[0].split("：", 1)[1])
    assert source["user_id"] == "20001" and source["event_id"] == "1"
    assert engine.memory.recent_history(SCOPE, "20001") == []
    assert engine.memory.recent_history(SCOPE, "20002") == []
