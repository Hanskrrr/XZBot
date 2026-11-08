"""Start/stop only the existing XZBot bridge and project-owned Lima VM.

No installation, download, login change, generic process kill, or user command
is accepted. Output is consumed by the core and never forwarded to the UI.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from start_onebot import ROOT, ensure_bridge, port_open, startup_guard, vm_prepared
from napcat_runtime import VM, LIMA_DIR, lima
from azusa_bot.runtime_layout import runtime_layout


def vm_state(root=ROOT, *, allow_partial=False):
    if not (root / "runtime/lima").exists():
        return "absent"
    if LIMA_DIR != (root / "runtime/lima").resolve():
        raise RuntimeError("runtime_ownership_unverified")
    if not vm_prepared(root):
        # Preparation may fail after Lima has created/started its VM. A Stop
        # remains available for that owned partial install, but Start still
        # requires the completed, pinned preparation marker.
        marker = LIMA_DIR / ".azusa-project"
        if not allow_partial or not marker.is_file() or marker.is_symlink() or marker.read_text().strip() != str(root):
            raise RuntimeError("runtime_ownership_unverified")
        if not (LIMA_DIR / VM).exists():
            return "absent"
        if not (LIMA_DIR / VM / "lima.yaml").is_file():
            raise RuntimeError("runtime_ownership_unverified")
    result = lima("list", "--format", "json", VM, capture=True)
    try:
        values = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if len(values) != 1 or values[0].get("name") != VM:
            raise ValueError()
        state = values[0]["status"]
        if state not in {"Running", "Stopped"}:
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("runtime_status_unknown") from None


def stop_bridge(root=ROOT):
    layout = runtime_layout(root)
    path = root / "data/onebot-launch.pid"
    if not path.exists():
        if port_open(layout.dashboard_port) or port_open(layout.onebot_port):
            raise RuntimeError("bridge_ownership_unverified")
        return
    if path.is_symlink() or path.stat().st_size > 30:
        raise RuntimeError("bridge_ownership_unverified")
    pid = int(path.read_text().strip())
    if pid <= 1:
        raise RuntimeError("bridge_ownership_unverified")
    command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
    if command.returncode:
        if port_open(layout.dashboard_port) or port_open(layout.onebot_port):
            raise RuntimeError("bridge_ownership_unverified")
        return
    expected = str(root / "scripts/run_onebot_astrbot.py")
    if expected not in command.stdout or str(root) not in command.stdout:
        raise RuntimeError("bridge_ownership_unverified")
    os.kill(pid, signal.SIGTERM)
    for _ in range(150):
        process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=5)
        if process.returncode or process.stdout.strip().startswith("Z"):
            return
        time.sleep(.1)
    raise RuntimeError("bridge_stop_timeout")


def stop():
    with startup_guard(ROOT):
        # Verify the VM before touching any process. No fallback to ~/.lima.
        state = vm_state(allow_partial=True)
        bridge_error = None
        try:
            stop_bridge()
        except Exception as error:
            bridge_error = error
        if state == "Running":
            lima("stop", VM, capture=True)
        if vm_state(allow_partial=True) not in {"Stopped", "absent"}:
            raise RuntimeError("runtime_not_stopped")
        if bridge_error:
            raise RuntimeError("bridge_not_stopped") from None


def start():
    with startup_guard(ROOT):
        if not vm_prepared(ROOT):
            raise RuntimeError("runtime_not_prepared")
        try:
            ensure_bridge(ROOT)
        except RuntimeError:
            raise RuntimeError("bridge_start_failed") from None
        state = vm_state()
        if state != "Running":
            lima("start", VM, capture=True)
        lima("shell", VM, "sudo", "systemctl", "start", "azusa-napcat", capture=True)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if port_open(runtime_layout(ROOT).napcat_port):
                return
            time.sleep(.5)
        raise RuntimeError("runtime_webui_unavailable")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    action = parser.parse_args().action
    try:
        start() if action == "start" else stop()
    except Exception as error:
        # Fixed categories only: never forward subprocess output or credentials.
        raise SystemExit({"runtime_not_prepared": 20, "bridge_start_failed": 21}.get(str(error), 1)) from None
