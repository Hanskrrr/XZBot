"""Pure/mock launcher checks: never start services, open a browser, or stop a VM."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import start_onebot as start
import stop_onebot as stop


def test_existing_core_is_reused_without_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(start, "core_ready", lambda _: True)
    monkeypatch.setattr(start, "spawn", lambda *a, **k: pytest.fail("must not restart core"))
    start.ensure_core(tmp_path)


def test_occupied_unknown_core_is_never_stopped_or_replaced(monkeypatch, tmp_path):
    monkeypatch.setattr(start, "core_ready", lambda _: False)
    monkeypatch.setattr(start, "port_open", lambda _: True)
    monkeypatch.setattr(start, "spawn", lambda *a, **k: pytest.fail("must not start over occupied port"))
    with pytest.raises(RuntimeError):
        start.ensure_core(tmp_path)


def test_existing_owned_bridge_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(start, "lock_held", lambda _: True)
    monkeypatch.setattr(start, "port_open", lambda _: True)
    monkeypatch.setattr(start, "spawn", lambda *a, **k: pytest.fail("must not restart bridge"))
    start.ensure_bridge(tmp_path)


def test_unprepared_vm_is_not_provisioned_or_started(monkeypatch, tmp_path):
    monkeypatch.setattr(start, "spawn", lambda *a, **k: pytest.fail("no automatic setup or start"))
    start.start_vm(tmp_path)


def test_vm_prepared_requires_project_marker_and_completed_setup(tmp_path):
    root = tmp_path / "project"; (root / "runtime").mkdir(parents=True)
    vm = tmp_path / "isolated"; (vm / "azusa").mkdir(parents=True)
    (root / "runtime/lima").symlink_to(vm, target_is_directory=True)
    (vm / "azusa/lima.yaml").write_text("test")
    (vm / ".azusa-project").write_text(str(root))
    assert not start.vm_prepared(root)
    (root / "runtime/napcat-prepared.json").write_text(json.dumps({"version": start.NAPCAT_VERSION, "sha256": start.NAPCAT_SHA256, "vm": start.NAPCAT_VM, "project_root": str(root), "lima_home": str(vm)}))
    assert start.vm_prepared(root)
    (vm / ".azusa-project").write_text("another-project")
    assert not start.vm_prepared(root)


def test_stop_only_disables_onebot_preserving_other_settings(tmp_path):
    (tmp_path / "config").mkdir()
    file = tmp_path / "config/local.json"
    config = {"onebot_enabled": True, "qq_enabled": True, "transport": "onebot", "onebot_owners": [{"user_id": "42"}]}
    file.write_text(json.dumps(config))
    stop.disable_replies(tmp_path)
    updated = json.loads(file.read_text())
    assert updated == {**config, "onebot_enabled": False}
    assert file.stat().st_mode & 0o077 == 0


def test_browser_token_uses_stdin_never_arguments_or_output(monkeypatch, tmp_path, capsys):
    secret = "TEST_ONLY_" + "x" * 40
    monkeypatch.setattr(start, "token", lambda *a: secret)
    calls = []
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(start.subprocess, "run", fake_run)
    start.open_management(tmp_path)
    assert calls[0][0] == ["/usr/bin/osascript", "-"]
    assert secret in calls[0][1]["input"]
    assert secret not in capsys.readouterr().out


def test_stop_does_not_shutdown_unowned_vm(monkeypatch, tmp_path):
    monkeypatch.setattr(stop, "ROOT", tmp_path)
    monkeypatch.setattr(stop, "disable_replies", lambda _: None)
    monkeypatch.setattr(stop, "owned_vm", lambda _: False)
    monkeypatch.setattr(stop, "spawn", lambda *a, **k: pytest.fail("must not stop other VM"))
    stop.main()


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_browser_failure_never_exposes_token_or_suggests_bare_url(monkeypatch, tmp_path, capsys, failure):
    secret = "TEST_ONLY_" + "z" * 40
    monkeypatch.setattr(start, "token", lambda *a: secret)
    def fake_run(args, **kwargs):
        if failure == "timeout":
            raise start.subprocess.TimeoutExpired(args, 15, stderr=secret)
        if failure == "oserror":
            raise OSError(secret)
        return SimpleNamespace(returncode=1, stderr=secret)
    monkeypatch.setattr(start.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as error:
        start.open_management(tmp_path)
    combined = str(error.value) + capsys.readouterr().out
    assert secret not in combined
    assert "http://" not in combined
    assert "XZBot 应用" in combined
    assert ".command" not in combined


def test_management_launcher_reuses_helpers_and_never_starts_qq(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("management_start_under_test", SCRIPTS / "start.py")
    management = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(management)
    calls = []
    monkeypatch.setattr(management, "ROOT", tmp_path)
    monkeypatch.setattr(management, "ensure_core", lambda root: calls.append(("core", root)))
    monkeypatch.setattr(management, "open_management", lambda root: calls.append(("open", root)))
    monkeypatch.setattr(start, "ensure_bridge", lambda *_: pytest.fail("management open must not start bridge"))
    monkeypatch.setattr(start, "start_vm", lambda *_: pytest.fail("management open must not start QQ"))
    management.main()
    assert calls == [("core", tmp_path), ("open", tmp_path)]


def test_cli_open_reuses_private_opener_and_never_starts_qq(monkeypatch, tmp_path):
    from azusa_bot import cli
    calls = []
    monkeypatch.setattr(cli, "root_path", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "argv", ["azusa_bot", "open"])
    monkeypatch.setattr(start, "ensure_core", lambda root: calls.append(("core", root)))
    monkeypatch.setattr(start, "open_management", lambda root: calls.append(("open", root)))
    monkeypatch.setattr(start, "ensure_bridge", lambda *_: pytest.fail("CLI open must not start bridge"))
    monkeypatch.setattr(start, "start_vm", lambda *_: pytest.fail("CLI open must not start QQ"))
    cli.main()
    assert calls == [("core", tmp_path), ("open", tmp_path)]


def test_core_authentication_also_requires_current_project_path(monkeypatch,tmp_path):
    import io
    (tmp_path/'data').mkdir()
    token=tmp_path/'data/admin.token';token.write_text('test-only-token');token.chmod(0o600)
    result={'version':'0.2.0','settings':{},'project_path':str(tmp_path/'old-copy')}
    class Opener:
        def open(self,*args,**kwargs):return io.BytesIO(json.dumps(result).encode())
    monkeypatch.setattr(start.urllib.request,'build_opener',lambda *_:Opener())
    assert not start.core_ready(tmp_path)
    result['project_path']=str(tmp_path.resolve())
    assert start.core_ready(tmp_path)


def test_start_guard_serializes_threads_without_replacing_lock(tmp_path):
    import threading
    entered=threading.Event();finished=threading.Event()
    def other():
        entered.set()
        with start.startup_guard(tmp_path):finished.set()
    with start.startup_guard(tmp_path):
        thread=threading.Thread(target=other);thread.start()
        assert entered.wait(1)
        assert not finished.wait(.05)
    thread.join(2)
    assert finished.is_set()
