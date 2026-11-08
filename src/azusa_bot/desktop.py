"""Fixed local desktop startup settings; never accepts a command from the browser."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import plistlib
import stat

from .settings import private_write
from .runtime_layout import runtime_layout

LABEL = "local.qqbot.service"


class DesktopService:
    def __init__(self, root: Path, home: Path | None = None):
        self.root = Path(root).resolve()
        layout = runtime_layout(self.root)
        self.label = LABEL + (".account." + self.root.name[:12] if layout.is_managed else "")
        self.home = home or Path.home()
        self.agent = self.home / "Library/LaunchAgents" / (self.label + ".plist")
        self.application = self.home / "Applications/XZBot.app"

    def _owned_agent(self):
        if self.agent.is_symlink():
            raise ValueError("desktop_foreign_launch_agent")
        if not self.agent.exists():
            return False
        try:
            data = plistlib.loads(self.agent.read_bytes())
        except (ValueError, OSError):
            raise ValueError("desktop_invalid_launch_agent") from None
        if (data.get("Label") != self.label or data.get("ProgramArguments") != self._arguments()):
            raise ValueError("desktop_foreign_launch_agent")
        return True

    def _arguments(self):
        return [str(self.root / ".venv/bin/python"), str(self.root / "scripts/service_supervisor.py")]

    def status(self):
        running = False
        lock_path = self.root / "data/service-supervisor.lock"
        if lock_path.is_file() and not lock_path.is_symlink():
            with lock_path.open("a") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    running = True
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
        return {"project_path": str(self.root), "application_path": str(self.application),
                "application_installed": self.application.is_dir(),
                "supervisor_running": running, "autostart_enabled": self._owned_agent(),
                "note": "关闭页面不停止服务。登录自启更改在下次登录生效；关闭自启不会关闭当前服务。"}

    def set_autostart(self, body):
        if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            raise ValueError("desktop_invalid_autostart")
        owned = self._owned_agent()
        if body["enabled"]:
            data = {"Label": self.label, "ProgramArguments": self._arguments(), "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
                    "WorkingDirectory": str(self.root), "Umask": 0o077,
                    "EnvironmentVariables": {"PYTHONPATH": str(self.root / "src"), "AZUSA_ROOT": str(self.root)},
                    "StandardOutPath": str(self.root / "data/service.log"),
                    "StandardErrorPath": str(self.root / "data/service.log")}
            self.agent.parent.mkdir(parents=True, exist_ok=True)
            private_write(self.agent, plistlib.dumps(data).decode())
            self.agent.chmod(stat.S_IRUSR | stat.S_IWUSR)
        elif owned:
            self.agent.unlink()
        return self.status()
