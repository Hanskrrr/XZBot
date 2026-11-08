"""Run supervisor control flow with fake processes; never launch QQ or a VM."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from azusa_bot.settings import private_write

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location("supervisor_test_" + name, PROJECT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def managed_root(tmp_path):
    manager = tmp_path.resolve() / "manager"
    root = manager / "accounts" / ("a" * 32)
    private_write(root / "config/instance.json", json.dumps({"schema": 1, "slot": 1, "manager_root": str(manager)}))
    private_write(root / "data/bot-control.json", json.dumps({"schema": 1, "state": "stopped",
        "epoch": "b" * 32, "updated_at": 0, "error_code": ""}))
    return root


def test_stopped_account_reuses_one_supervisor_without_deadlock_or_qq(tmp_path, monkeypatch):
    helper, supervisor = load("account_instance_action"), load("service_supervisor")
    root = managed_root(tmp_path)
    for module in (helper, supervisor):
        monkeypatch.setattr(module, "ROOT", root)
    stop = threading.Event()
    supervisor_checked_core = threading.Event()
    # Preserve the actual lifetime/startup locks and activity gate. Only process
    # launch, OS signals and external service operations are replaced.
    monkeypatch.setattr(supervisor, "threading", SimpleNamespace(Event=lambda: stop))
    monkeypatch.setattr(supervisor, "signal", SimpleNamespace(signal=lambda *a: None, SIGTERM=15, SIGINT=2))
    monkeypatch.setattr(supervisor, "ensure_core", lambda _: supervisor_checked_core.set())
    monkeypatch.setattr(supervisor, "core_ready", lambda _: True)
    monkeypatch.setattr(supervisor, "ensure_bridge", lambda _: pytest.fail("stopped account must not start bridge"))
    monkeypatch.setattr(supervisor, "start_vm", lambda _: pytest.fail("stopped account must not start VM"))
    monkeypatch.setattr(supervisor, "vm_prepared", lambda _: pytest.fail("closed activity must short circuit VM checks"))
    monkeypatch.setattr(helper, "ensure_core", lambda _: None)
    opened = []
    monkeypatch.setattr(helper, "open_management", lambda root: opened.append(root))
    launched, threads = [], []
    errors = []
    def run_supervisor():
        try:
            supervisor.main()
        except BaseException as error:
            errors.append(error)
    def fake_spawn(directory, arguments, logfile):
        launched.append((directory, arguments, logfile))
        thread = threading.Thread(target=run_supervisor)
        threads.append(thread)
        thread.start()
        return SimpleNamespace(poll=lambda: None if thread.is_alive() else 0)
    monkeypatch.setattr(helper, "spawn", fake_spawn)
    try:
        for operation in ("ensure-core", "open", "ensure-core"):
            monkeypatch.setattr(helper.sys, "argv", ["helper", operation])
            helper.main()
        assert supervisor_checked_core.wait(2), "supervisor must resume after helper releases startup_guard"
        assert len(launched) == 1
        assert launched[0] == (root, [str(root / ".venv/bin/python"),
                                    str(root / "scripts/service_supervisor.py")], "service.log")
        assert opened == [root]
        assert helper.lock_held(root / "data/service-supervisor.lock")
        assert not list(tmp_path.rglob("*.plist")), "no login agent should be installed"
        assert not errors
    finally:
        stop.set()
        for thread in threads:
            thread.join(3)
        assert all(not thread.is_alive() for thread in threads)


def test_failed_supervisor_launch_only_terminates_new_child(tmp_path, monkeypatch):
    helper = load("account_instance_action")
    root = managed_root(tmp_path)
    calls = []
    child = SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append("terminate"),
                            wait=lambda **_: calls.append("wait"))
    monkeypatch.setattr(helper, "spawn", lambda *a: child)
    monkeypatch.setattr(helper, "wait_ready", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="account_supervisor_unavailable"):
        helper.ensure_supervisor(root)
    assert calls == ["terminate", "wait"]


def test_primary_account_cannot_use_child_helper(tmp_path, monkeypatch):
    helper = load("account_instance_action")
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper.sys, "argv", ["helper", "ensure-core"])
    monkeypatch.setattr(helper, "ensure_core", lambda _: pytest.fail("primary account must be unchanged"))
    monkeypatch.setattr(helper, "spawn", lambda *a: pytest.fail("primary account must be unchanged"))
    with pytest.raises(RuntimeError, match="account_manager_only"):
        helper.main()
