from pathlib import Path
import fcntl
import plistlib
import pytest
from azusa_bot.desktop import DesktopService


def installer(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("desktop_installer_test", Path(__file__).parents[1] / "scripts/install_desktop.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root = tmp_path / "project"; root.mkdir()
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(Path, "home", lambda: home)
    return module, root, home


def test_owned_desktop_renames_without_duplicate_launchers(tmp_path, monkeypatch):
    module, root, home = installer(tmp_path, monkeypatch)
    old = home / "Applications/QQBot.app"
    marker = old / "Contents/Resources/qqbot-project.txt"
    marker.parent.mkdir(parents=True); marker.write_text(str(root)+'\n')
    (root / "QQBot.app").symlink_to(old, target_is_directory=True)
    target = module.install()
    assert target == home / "Applications/XZBot.app"
    assert not old.exists() and not old.is_symlink()
    assert (root / "QQBot.app").resolve() == (root / "XZBot.app").resolve() == target
    info = plistlib.loads((target / "Contents/Info.plist").read_bytes())
    assert info['CFBundleDisplayName'] == info['CFBundleExecutable'] == 'XZBot'
    assert 'desktop_launcher.py' in (target / 'Contents/MacOS/XZBot').read_text()
    assert module.install() == target


def test_foreign_xzbot_app_is_not_overwritten(tmp_path, monkeypatch):
    module, root, home = installer(tmp_path, monkeypatch)
    target = home / 'Applications/XZBot.app'; target.mkdir(parents=True)
    (target / 'foreign').write_text('preserve')
    with pytest.raises(RuntimeError): module.install()
    assert (target / 'foreign').read_text() == 'preserve'


def test_autostart_has_only_fixed_owned_command_and_no_secrets(tmp_path):
    root = tmp_path / "QQBot"; root.mkdir()
    service = DesktopService(root, tmp_path / "home")
    assert not service.status()["autostart_enabled"]
    assert service.set_autostart({"enabled": True})["autostart_enabled"]
    data = plistlib.loads(service.agent.read_bytes())
    assert data["ProgramArguments"] == [str(root / ".venv/bin/python"), str(root / "scripts/service_supervisor.py")]
    assert data["RunAtLoad"] and data["Umask"] == 0o077
    assert not service.agent.stat().st_mode & 0o077
    assert not service.set_autostart({"enabled": False})["autostart_enabled"]
    with pytest.raises(ValueError): service.set_autostart({"enabled": True, "command": "unsafe"})


def test_foreign_agent_and_symlink_are_never_overwritten(tmp_path):
    service = DesktopService(tmp_path / "QQBot", tmp_path / "home")
    service.agent.parent.mkdir(parents=True)
    original = plistlib.dumps({"Label": "someone.else", "ProgramArguments": ["other"]})
    service.agent.write_bytes(original)
    with pytest.raises(ValueError): service.set_autostart({"enabled": True})
    with pytest.raises(ValueError): service.set_autostart({"enabled": False})
    assert service.agent.read_bytes() == original
    service.agent.unlink(); target = tmp_path / "foreign"; target.write_bytes(original)
    service.agent.symlink_to(target)
    with pytest.raises(ValueError): service.set_autostart({"enabled": True})
    assert target.read_bytes() == original


def test_running_status_requires_actual_supervisor_lock(tmp_path):
    service = DesktopService(tmp_path, tmp_path / "home")
    (tmp_path / "data").mkdir()
    lock = tmp_path / "data/service-supervisor.lock"
    with lock.open("a") as handle:
        assert not service.status()["supervisor_running"]
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert service.status()["supervisor_running"]
