"""Fixed host operations used by the authenticated whole-bot power control."""
from __future__ import annotations
from .host import python_at

import asyncio
import os
from pathlib import Path
import signal
from .native_process import terminate_helper

from .bot_control import BotPowerError
from .napcat_admin import NapCatAdmin, NapCatError
from .qq_runtime import make_admin
from .settings import read_settings


class NativeBotRuntime:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    async def _run(self, action):
        if action not in {"start", "stop"}:
            raise BotPowerError("power_runtime_unavailable")
        python = python_at(self.root / ".venv")
        script = self.root / "scripts/bot_runtime_action.py"
        if not python.is_file() or not script.is_file() or script.is_symlink():
            raise BotPowerError("power_runtime_unavailable")
        try:
            process = await asyncio.create_subprocess_exec(
                str(python), str(script), action, cwd=self.root,
                env={**os.environ, "AZUSA_ROOT": str(self.root), "PYTHONPATH": str(self.root / "src")},
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        except OSError:
            raise BotPowerError("power_runtime_unavailable") from None
        try:
            code = await asyncio.wait_for(process.wait(), timeout=180)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            # Only this newly created helper process group can be terminated.
            try:
                terminate_helper(process)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    terminate_helper(process,force=True)
                except ProcessLookupError:
                    pass
                await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise BotPowerError("power_runtime_timeout") from None
        if code != 0:
            raise BotPowerError({20: "power_runtime_not_prepared", 21: "power_bridge_start_failed",22:"power_other_adapter_running"}.get(
                code, "power_runtime_unavailable"))

    async def stop(self):
        await self._run("stop")

    async def start(self):
        settings = read_settings(self.root)
        if settings.transport != "onebot":
            # The preserved official adapter has no Lima dependency. Its own
            # authenticated incoming path is still protected by the power gate.
            return
        if not settings.onebot_self_id:
            raise BotPowerError("power_qq_binding_required")
        await self._run("start")
        last = "power_qq_login_required"
        admin = make_admin(self.root)
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline:
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                status = await asyncio.wait_for(admin.status(), timeout=min(8, remaining))
            except (NapCatError, asyncio.TimeoutError):
                last = "power_runtime_unavailable"
            else:
                if status.get("logged_in") and not status.get("offline"):
                    latest = read_settings(self.root)
                    if (latest.transport != "onebot" or not latest.onebot_self_id
                            or status.get("qq_number") != latest.onebot_self_id):
                        raise BotPowerError("power_qq_identity_mismatch")
                    return
                last = "power_qq_login_required"
            await asyncio.sleep(1)
        raise BotPowerError(last)
