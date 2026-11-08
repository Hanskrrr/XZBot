"""XZBot.app entry: start/reuse the background service and open an authenticated UI."""
from pathlib import Path
import os
import sys
import subprocess

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from start_onebot import core_ready, lock_held, open_management, spawn, wait_ready


def main():
    os.umask(0o077)
    if not lock_held(ROOT / "data/service-supervisor.lock"):
        spawn(ROOT, [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/service_supervisor.py")], "service.log")
    if not wait_ready(lambda: core_ready(ROOT), timeout=45):
        raise RuntimeError("服务尚未启动成功")
    open_management(ROOT)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fixed user-facing text only; no exception text, token, or user content.
        subprocess.run(["/usr/bin/osascript", "-"], input='display alert "XZBot 暂时无法打开" message "请检查 QQBot/data/service.log，或稍后重新打开 XZBot 应用。"\n',
                       text=True, capture_output=True, timeout=20)
        raise SystemExit(1) from None
