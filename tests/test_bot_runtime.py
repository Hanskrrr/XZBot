"""Host lifecycle checks use fake processes and temporary ownership markers."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from azusa_bot.bot_control import BotPowerError
from azusa_bot.bot_runtime import NativeBotRuntime
from azusa_bot.settings import Settings, write_settings

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("runtime_action_test", SCRIPTS / "bot_runtime_action.py")
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)


def test_vm_status_does_not_accept_foreign_or_unknown_instance(tmp_path, monkeypatch):
    (tmp_path / "runtime/lima").mkdir(parents=True)
    monkeypatch.setattr(action, "LIMA_DIR", tmp_path / "runtime/lima")
    monkeypatch.setattr(action, "vm_prepared", lambda root: False)
    lima = AsyncMock()
    monkeypatch.setattr(action, "lima", lima)
    with pytest.raises(RuntimeError, match="ownership"):
        action.vm_state(tmp_path)
    lima.assert_not_called()
    monkeypatch.setattr(action, "vm_prepared", lambda root: True)
    monkeypatch.setattr(action, "lima", lambda *a, **k: SimpleNamespace(stdout=json.dumps({"name": "foreign", "status": "Running"})))
    with pytest.raises(RuntimeError, match="unknown"):
        action.vm_state(tmp_path)


@pytest.mark.parametrize("state", ["Running", "Stopped"])
def test_owned_vm_state_uses_only_project_lima(tmp_path, monkeypatch, state):
    (tmp_path / "runtime/lima").mkdir(parents=True)
    monkeypatch.setattr(action, "LIMA_DIR", tmp_path / "runtime/lima")
    monkeypatch.setattr(action, "vm_prepared", lambda root: True)
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps({"name": action.VM, "status": state}))
    monkeypatch.setattr(action, "lima", run)
    assert action.vm_state(tmp_path) == state
    assert calls == [(("list", "--format", "json", action.VM), {"capture": True})]


def test_bridge_stop_never_kills_unrelated_pid(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/onebot-launch.pid").write_text("12345")
    monkeypatch.setattr(action.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="/usr/bin/python unrelated.py"))
    killed = []
    monkeypatch.setattr(action.os, "kill", lambda *a: killed.append(a))
    with pytest.raises(RuntimeError, match="ownership"):
        action.stop_bridge(tmp_path)
    assert not killed


@pytest.mark.asyncio
async def test_native_start_requires_same_logged_account(tmp_path, monkeypatch):
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id="10001"))
    runtime = NativeBotRuntime(tmp_path)
    runtime._run = AsyncMock()
    monkeypatch.setattr("azusa_bot.bot_runtime.NapCatAdmin.status", AsyncMock(return_value={
        "logged_in": True, "offline": False, "qq_number": "10002"}))
    with pytest.raises(BotPowerError, match="identity_mismatch"):
        await runtime.start()
    runtime._run.assert_awaited_once_with("start")


@pytest.mark.asyncio
async def test_native_stop_and_matching_login_use_fixed_actions(tmp_path, monkeypatch):
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id="10001"))
    runtime = NativeBotRuntime(tmp_path)
    runtime._run = AsyncMock()
    monkeypatch.setattr("azusa_bot.bot_runtime.NapCatAdmin.status", AsyncMock(return_value={
        "logged_in": True, "offline": False, "qq_number": "10001"}))
    await runtime.stop()
    await runtime.start()
    assert [c.args for c in runtime._run.await_args_list] == [("stop",), ("start",)]


@pytest.mark.asyncio
async def test_native_control_refuses_missing_installation_and_arbitrary_actions(tmp_path):
    runtime = NativeBotRuntime(tmp_path)
    for operation in ("start", "stop", "arbitrary shell"):
        with pytest.raises(BotPowerError, match="unavailable"):
            await runtime._run(operation)


async def test_unbound_account_fails_before_starting_any_runtime(tmp_path):
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id=""))
    runtime = NativeBotRuntime(tmp_path)
    runtime._run = AsyncMock()
    with pytest.raises(BotPowerError, match="power_qq_binding_required"):
        await runtime.start()
    runtime._run.assert_not_awaited()


@pytest.mark.parametrize("code,error", [(20,"power_runtime_not_prepared"), (21,"power_bridge_start_failed"), (1,"power_runtime_unavailable")])
async def test_helper_failure_reports_only_fixed_category(tmp_path, monkeypatch, code, error):
    for name in [".venv/bin/python", "scripts/bot_runtime_action.py"]:
        p = tmp_path / name; p.parent.mkdir(parents=True, exist_ok=True); p.touch()
    monkeypatch.setattr("azusa_bot.bot_runtime.asyncio.create_subprocess_exec",
                        AsyncMock(return_value=SimpleNamespace(wait=AsyncMock(return_value=code))))
    with pytest.raises(BotPowerError, match=error):
        await NativeBotRuntime(tmp_path)._run("start")
