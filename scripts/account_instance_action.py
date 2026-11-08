"""Explicit account controls; preparation never enables automatic QQ replies."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from azusa_bot.runtime_layout import runtime_layout
from azusa_bot.bot_control import read_control
from azusa_bot.settings import private_write
from start_onebot import ensure_core, open_management, startup_guard, vm_prepared, lock_held, spawn, wait_ready


def ensure_supervisor(root):
    """Called under startup_guard: reuse one detached supervisor per account."""
    lock = root / "data/service-supervisor.lock"
    if lock_held(lock):
        return
    child = spawn(root, [str(root / ".venv/bin/python"),
                        str(root / "scripts/service_supervisor.py")], "service.log")
    # The supervisor takes its lifetime lock BEFORE startup_guard. Wait only
    # for that lock, never its service startup (which needs our current guard).
    # Once the helper releases the guard, it monitors the existing core. Its
    # existing activity gate leaves stopped/error accounts' QQ runtime closed.
    if not wait_ready(lambda: lock_held(lock), child, timeout=10):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        raise RuntimeError("account_supervisor_unavailable")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["open", "ensure-core", "prepare"])
    action = parser.parse_args().action
    if not runtime_layout(ROOT).is_managed:
        raise RuntimeError("account_manager_only")
    with startup_guard(ROOT):
        ensure_core(ROOT)
        ensure_supervisor(ROOT)
        if action == "prepare":
            state = read_control(ROOT)
            if state["enabled"] or state["busy"]:
                raise RuntimeError("account_already_running")
            # The QQ VM is running for login, while automatic replies remain
            # closed. Set this before setup: failed preparation may already
            # have started an owned VM, and Stop must not short-circuit.
            private_write(ROOT / "data/bot-control.json", json.dumps({"schema": 1, "state": "error",
                "epoch": uuid.uuid4().hex, "updated_at": time.time(), "error_code": "power_qq_login_required"}))
            if not vm_prepared(ROOT):
                subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/napcat_runtime.py"), "setup"], check=True)
            subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/napcat_runtime.py"), "start"], check=True)
        if action == "open":
            open_management(ROOT)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("account_operation_failed") from None
