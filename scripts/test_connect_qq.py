"""Guard tests; never launch AstrBot, connect QQ or inspect real credentials."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("connect_qq_under_test", Path(__file__).with_name("connect_qq.py"))
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


def test_requires_operator_assertion(tmp_path):
    with pytest.raises(script.CutoverError):
        script.check_old_dsh_stopped(tmp_path, False)


def test_running_dsh_blocks_even_asserted(monkeypatch, tmp_path):
    monkeypatch.setattr(script, "process_snapshot", lambda: ["/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness"])
    with pytest.raises(script.CutoverError):
        script.check_old_dsh_stopped(tmp_path, True)


def test_stopped_main_or_explicit_disable(monkeypatch, tmp_path):
    monkeypatch.setattr(script, "process_snapshot", lambda: ["/sbin/launchd", "/bin/zsh"])
    script.check_old_dsh_stopped(tmp_path, True)
    patch = tmp_path / ".dsh/profiles/desktop/cordis.patch.yml"
    patch.parent.mkdir(parents=True)
    patch.write_text("- id: im-qqbot\n  disabled: true\n")
    monkeypatch.setattr(script, "process_snapshot", lambda: pytest.fail("explicit disabled entry suffices"))
    script.check_old_dsh_stopped(tmp_path, True)


def test_unknown_process_check_fails_closed(monkeypatch):
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(script.CutoverError):
        script.process_snapshot()


def valid():
    from azusa_bot.settings import Settings, Owner
    settings = Settings(app_id="app", provider_mode="cloud", allowed_dm_users=["owner"], owners=[Owner(kind="dm",scope_id="owner",user_id="owner")])
    credentials = SimpleNamespace(qq_app_id="app", qq_secret="secret-do-not-write", deepseek_key="secret-deepseek", fish_key="", fish_reference="")
    return settings, credentials


def test_binding_mismatch_and_unlisted_owner():
    settings, credentials = valid()
    script.validate_bindings(settings, credentials)
    settings.app_id = "wrong"
    with pytest.raises(script.CutoverError):
        script.validate_bindings(settings, credentials)
    settings.app_id = "app"
    settings.allowed_dm_users = ["someone-else"]
    with pytest.raises(script.CutoverError):
        script.validate_bindings(settings, credentials)


def test_busy_port_prevents_credentials_and_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(script, "check_old_dsh_stopped", lambda *a: None)
    monkeypatch.setattr(script, "port_listening", lambda p: True)
    monkeypatch.setattr(script, "load_credentials", lambda *a: pytest.fail("must not read credentials"))
    with pytest.raises(script.CutoverError):
        script.prepare(tmp_path, True)
    assert list(tmp_path.iterdir()) == []


def test_unverified_source_revision_rejected(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/astrbot-source.path").write_text(str(tmp_path / "source"))
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="wrong-revision\n"))
    with pytest.raises(script.CutoverError):
        script.verify_versions(tmp_path)


def test_check_and_failed_exec_do_not_leave_enabled(monkeypatch, tmp_path):
    settings, credentials = valid()
    bridge_path = tmp_path / "bridge.json"
    original = json.dumps({"enabled": False, "app_id": ""})
    bridge_path.write_text(original)
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.setattr(script, "prepare", lambda *a: (settings, credentials, bridge_path, original, json.loads(original)))
    writes = []
    monkeypatch.setattr(script, "write_settings", lambda root, value: writes.append(value.qq_enabled))
    assert script.main(["--old-dsh-stopped", "--check"]) == 0
    assert not writes and bridge_path.read_text() == original
    def failed_exec(path, argv, env):
        assert "secret-do-not-write" not in str(argv)
        assert env["AZUSA_QQ_APP_SECRET"] == "secret-do-not-write"
        assert "secret-do-not-write" not in bridge_path.read_text()
        raise OSError()
    monkeypatch.setattr(script.os, "execve", failed_exec)
    assert script.main(["--old-dsh-stopped"]) == 1
    assert writes == [True, False]
    assert bridge_path.read_text() == original
