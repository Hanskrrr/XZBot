"""Whole-bot power transitions without a host VM, network or real accounts."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from azusa_bot.app import create_app
from azusa_bot.bot_control import BotPowerController, BotPowerError, activity_allowed, read_control
from azusa_bot.core import Engine, Incoming
from azusa_bot.profiles import ProfileEngineManager
from azusa_bot.settings import Settings, private_write, write_settings


def root_at(path):
    (path / "content").mkdir(parents=True)
    (path / "content/persona.md").write_text("# Synthetic character\nPrivate offline test.")
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
        onebot_allowed_groups=["30001"], onebot_allowed_dm_users=["20001"], min_interval_seconds=0)
    write_settings(path, settings)
    return settings


def event(number="1", **changes):
    return Incoming(**dict(dict(transport="onebot", app_id="10001", kind="group", scope_id="30001",
        user_id="20001", event_id=number, text="你好", mentioned=True), **changes))


def controller(root, invalidate=lambda: None):
    power = BotPowerController(root, invalidate)
    power.runtime_start = AsyncMock()
    power.runtime_stop = AsyncMock()
    return power


def test_legacy_state_and_corrupt_private_file_fail_closed(tmp_path):
    assert activity_allowed(tmp_path)
    private_write(tmp_path / "data/bot-control.json", "broken")
    assert not activity_allowed(tmp_path)
    assert read_control(tmp_path)["error_code"] == "power_state_invalid"
    power = controller(tmp_path)
    power._write("running")
    (tmp_path / "data/bot-control.json").chmod(0o644)
    assert not activity_allowed(tmp_path)


def test_control_symlink_is_never_accepted(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/bot-control.json").symlink_to(tmp_path / "missing")
    assert not activity_allowed(tmp_path)


@pytest.mark.asyncio
async def test_stop_gate_precedes_runtime_shutdown_and_survives_restart(tmp_path):
    power = controller(tmp_path)
    before = read_control(tmp_path)["epoch"]
    seen = []
    async def stop():
        seen.append(activity_allowed(tmp_path))
        assert read_control(tmp_path)["state"] == "stopping"
    power.runtime_stop = stop
    result = await power.stop()
    assert seen == [False] and result["state"] == "stopped"
    assert result["epoch"] != before
    assert controller(tmp_path).status()["enabled"] is False


@pytest.mark.asyncio
async def test_start_gate_opens_only_after_success_and_failure_stays_closed(tmp_path):
    power = controller(tmp_path)
    await power.stop()
    old_epoch = power.status()["epoch"]
    async def fail():
        assert not activity_allowed(tmp_path)
        raise BotPowerError("power_qq_login_required")
    power.runtime_start = fail
    with pytest.raises(BotPowerError, match="power_qq_login_required"):
        await power.start()
    assert power.status()["state"] == "error"
    assert not activity_allowed(tmp_path)
    power.runtime_start = AsyncMock()
    await power.start()
    assert activity_allowed(tmp_path)
    assert not activity_allowed(tmp_path, old_epoch)


@pytest.mark.asyncio
async def test_repeated_concurrent_stop_is_idempotent(tmp_path):
    power = controller(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def stop():
        calls.append(1); entered.set(); await release.wait()
    power.runtime_stop = stop
    first = asyncio.create_task(power.stop())
    await entered.wait()
    second = asyncio.create_task(power.stop())
    release.set()
    assert (await first)["state"] == "stopped"
    assert (await second)["state"] == "stopped"
    assert calls == [1]


def test_interrupted_transition_does_not_reopen_on_process_restart(tmp_path):
    power = controller(tmp_path)
    power._write("starting")
    restored = controller(tmp_path)
    assert restored.status()["state"] == "error"
    assert restored.status()["error_code"] == "power_transition_interrupted"
    assert not activity_allowed(tmp_path)


class BlockingProvider:
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def complete(self, messages, *, summary=False):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if summary:
            history = json.loads(messages[-1]["content"])
            return {"memories": [{"text": "用户喜欢合成测试", "source_ids": [history[0]["id"]]}]}
        return {"text": "合成回复", "mode": "text"}


@pytest.mark.asyncio
async def test_inflight_reply_cannot_reappear_after_stop_and_start(tmp_path):
    root_at(tmp_path)
    engine = Engine(tmp_path)
    provider = BlockingProvider(); engine.provider_override = provider
    power = controller(tmp_path, engine.invalidate_groups)
    task = asyncio.create_task(engine.handle(event()))
    await provider.entered.wait()
    await power.stop(); await power.start()
    provider.release.set()
    old = await task
    assert old["mode"] == "none"
    fresh = await engine.handle(event("2"))
    assert fresh["mode"] == "text" and provider.calls == 2
    engine.memory.close(); engine.groups.close()


@pytest.mark.asyncio
async def test_queued_request_from_old_epoch_does_not_enter_provider_after_restart(tmp_path):
    root_at(tmp_path)
    engine = Engine(tmp_path)
    provider = BlockingProvider(); provider.release.set(); engine.provider_override = provider
    engine._capacity = asyncio.Semaphore(0)
    power = controller(tmp_path, engine.invalidate_groups)
    task = asyncio.create_task(engine.handle(event()))
    for _ in range(3):
        await asyncio.sleep(0)
    await power.stop(); await power.start()
    engine._capacity.release()
    result = await task
    assert result["mode"] == "none" and provider.calls == 0
    engine.memory.close(); engine.groups.close()


@pytest.mark.asyncio
async def test_generated_voice_is_discarded_if_power_cycles_during_synthesis(tmp_path):
    settings = root_at(tmp_path)
    settings.voice_enabled = settings.voice_authorized = True
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    media = tmp_path / "data/media" / ("a" * 32 + ".wav")
    class Voice:
        async def complete(self, messages, **kwargs):
            return {"text": "合成语音", "mode": "voice"}
        async def synthesize(self, text):
            entered.set(); await release.wait()
            media.parent.mkdir(exist_ok=True)
            media.write_bytes(b"synthetic")
            return media
    engine.provider_override = Voice()
    power = controller(tmp_path, engine.invalidate_groups)
    task = asyncio.create_task(engine.handle(event()))
    await entered.wait()
    await power.stop(); await power.start(); release.set()
    assert (await task)["mode"] == "none"
    assert not media.exists()
    engine.memory.close(); engine.groups.close()


@pytest.mark.asyncio
async def test_stopped_messages_do_not_observe_or_call_model(tmp_path):
    root_at(tmp_path)
    engine = Engine(tmp_path)
    engine.memory.set_group_collection("onebot:10001:group:30001", True)
    provider = BlockingProvider(); engine.provider_override = provider
    await controller(tmp_path, engine.invalidate_groups).stop()
    result = await engine.handle(event(mentioned=False, unprompted=True))
    assert result["reason"] == "bot_stopped" and provider.calls == 0
    assert engine.memory.recent_history("onebot:10001:group:30001", "20001") == []
    assert (await engine.summarize_due())["status"] in {"profile_changed", "bot_stopped"}
    engine.memory.close(); engine.groups.close()


@pytest.mark.asyncio
async def test_summary_inflight_is_discarded_across_power_cycle(tmp_path):
    settings = root_at(tmp_path)
    engine = Engine(tmp_path); provider = BlockingProvider()
    scope = "onebot:10001:group:30001"
    engine.memory.set_consent(scope, "20001", True)
    engine.memory.add_history(scope, "20001", "user", "我喜欢合成测试", "1")
    power = controller(tmp_path, engine.invalidate_groups)
    task = asyncio.create_task(engine.summarize(scope, "20001", provider, settings, force=True))
    await provider.entered.wait()
    await power.stop(); await power.start(); provider.release.set()
    result = await task
    assert result["status"] != "saved"
    assert engine.memory.list_memories(scope, "20001") == []
    engine.memory.close(); engine.groups.close()


@pytest.mark.asyncio
async def test_profile_switch_while_stopped_cannot_reenable_and_cancels_prepared(tmp_path):
    root_at(tmp_path)
    engine = ProfileEngineManager(tmp_path)
    prepared = await engine.handle(event())
    assert engine.authorize_reply(prepared["reply_id"])
    power = controller(tmp_path, engine.invalidate_activity)
    original = (tmp_path / "config/local.json").read_bytes()
    await power.stop()
    profile = engine.profiles.create({"name": "Another synthetic profile"})
    engine.activate(profile["id"])
    assert (await engine.handle(event("2")))["reason"] == "bot_stopped"
    await power.start()
    assert not engine.authorize_reply(prepared["reply_id"])
    assert (tmp_path / "config/local.json").read_bytes() == original
    engine.close()


def test_power_api_is_admin_only_and_not_overridden_by_stale_profile_or_settings(tmp_path):
    root_at(tmp_path)
    app = create_app(tmp_path)
    power = app.state.bot_power
    power.runtime_start, power.runtime_stop = AsyncMock(), AsyncMock()
    headers = {"Authorization": "Bearer " + (tmp_path / "data/admin.token").read_text().strip()}
    client = TestClient(app)
    assert client.get("/admin/bot-power").status_code == 401
    assert client.post("/admin/bot-power/stop", json={}).status_code == 401
    result = client.post("/admin/bot-power/stop", headers=headers | {"X-XZBot-Profile": "stale"}, json={})
    assert result.status_code == 200 and not result.json()["enabled"]
    settings = client.get("/admin/settings", headers=headers).json()
    assert client.put("/admin/settings", headers=headers, json=settings).status_code == 200
    assert not client.get("/admin/bot-power", headers=headers).json()["enabled"]
    assert client.post("/admin/bot-power/start", headers=headers, json={}).json()["enabled"]
    power.runtime_start.assert_awaited_once(); power.runtime_stop.assert_awaited_once()
    app.state.engine.close()
