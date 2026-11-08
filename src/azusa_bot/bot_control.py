"""Persistent whole-bot power gate, independent of editable provider settings.

The management process stays alive. Closing the bot disables activity first,
then asks the host adapter to stop the QQ runtime. Only an explicit successful
start opens a new activity epoch; stale requests can never survive that cycle.
"""
from __future__ import annotations
from .host import private_info, private_path, protect, link_directory
from .host import safe_open, NOFOLLOW

import asyncio
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .settings import private_write

STATES = {"running", "starting", "stopping", "stopped", "error"}
ERRORS = {"", "power_state_invalid", "power_transition_interrupted", "power_start_failed",
          "power_stop_failed", "power_runtime_unconfigured", "power_qq_login_required",
          "power_qq_identity_mismatch", "power_runtime_unavailable", "power_runtime_timeout",
          "power_qq_binding_required", "power_runtime_not_prepared", "power_bridge_start_failed", "power_other_adapter_running"}


def read_control(root: Path) -> dict:
    path = Path(root) / "data/bot-control.json"
    state = {"state": "running", "epoch": "legacy", "updated_at": 0., "error_code": ""}
    try:
        fd = safe_open(path, os.O_RDONLY | NOFOLLOW)
    except FileNotFoundError:
        if path.is_symlink():
            state.update(state="error", epoch="invalid", error_code="power_state_invalid")
    except OSError:
        state.update(state="error", epoch="invalid", error_code="power_state_invalid")
    else:
        try:
            with os.fdopen(fd, "r") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or not private_info(info,stream.fileno()) or info.st_size > 4096):
                    raise ValueError()
                data = json.load(stream)
            if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("state") not in STATES
                    or not re.fullmatch(r"[a-f0-9]{32}", str(data.get("epoch", "")))
                    or data.get("error_code", "") not in ERRORS
                    or type(data.get("updated_at")) not in {int, float}):
                raise ValueError()
            state = {key: data[key] for key in ("state", "epoch", "updated_at", "error_code")}
        except (OSError, ValueError, KeyError, TypeError):
            state.update(state="error", epoch="invalid", error_code="power_state_invalid")
    return dict(state, enabled=state["state"] == "running", busy=state["state"] in {"starting", "stopping"})


def activity_allowed(root: Path, epoch: str | None = None) -> bool:
    state = read_control(root)
    return state["enabled"] and (epoch is None or epoch == state["epoch"])


class BotPowerError(RuntimeError):
    pass


async def _unconfigured():
    raise BotPowerError("power_runtime_unconfigured")


class BotPowerController:
    def __init__(self, root: Path, invalidate):
        self.root, self.invalidate = Path(root), invalidate
        self.lock = asyncio.Lock()
        self.runtime_stop = _unconfigured
        self.runtime_start = _unconfigured
        state = self.status()
        if state["busy"]:
            self._write("error", "power_transition_interrupted")

    def status(self):
        return read_control(self.root)

    def _write(self, state, error=""):
        if state not in STATES or error not in ERRORS:
            raise ValueError("power_state_invalid")
        private_write(self.root / "data/bot-control.json", json.dumps({"schema": 1, "state": state,
            "epoch": uuid.uuid4().hex, "updated_at": time.time(), "error_code": error}))

    async def stop(self):
        async with self.lock:
            if self.status()["state"] == "stopped":
                return self.status()
            self._write("stopping")
            try:
                self.invalidate()
                await self.runtime_stop()
            except asyncio.CancelledError:
                self._write("error", "power_transition_interrupted")
                raise
            except Exception as exc:
                code = str(exc) if isinstance(exc, BotPowerError) and str(exc) in ERRORS else "power_stop_failed"
                self._write("error", code)
                raise BotPowerError(code) from None
            self._write("stopped")
            return self.status()

    async def start(self):
        async with self.lock:
            if self.status()["state"] == "running":
                return self.status()
            self._write("starting")
            try:
                self.invalidate()
                await self.runtime_start()
                self.invalidate()
            except asyncio.CancelledError:
                self._write("error", "power_transition_interrupted")
                raise
            except Exception as exc:
                code = str(exc) if isinstance(exc, BotPowerError) and str(exc) in ERRORS else "power_start_failed"
                self._write("error", code)
                raise BotPowerError(code) from None
            self._write("running")
            return self.status()
