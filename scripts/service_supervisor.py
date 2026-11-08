"""User-session supervisor for the two owned services; no terminal window needed."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from start_onebot import core_ready, ensure_core, ensure_bridge, lock_held, port_open, start_vm, vm_prepared, startup_guard
from azusa_bot.settings import private_write
from azusa_bot.bot_control import activity_allowed
from azusa_bot.host import file_locks as fcntl
from azusa_bot.runtime_layout import runtime_layout


def main():
    os.umask(0o077)
    layout = runtime_layout(ROOT)
    (ROOT / "data").mkdir(mode=0o700, exist_ok=True)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with (ROOT / "data/service-supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        private_write(ROOT / "data/service-supervisor.pid", str(os.getpid()))
        with startup_guard(ROOT):
            ensure_core(ROOT)
            if activity_allowed(ROOT):
                ensure_bridge(ROOT)
        # Start the already-prepared VM once. A later explicit QQ stop remains stopped.
        if activity_allowed(ROOT) and vm_prepared(ROOT) and not port_open(layout.napcat_port):
            try:
                with startup_guard(ROOT):
                    if activity_allowed(ROOT):
                        start_vm(ROOT)
            except Exception:
                print("隔离 QQ 环境启动未完成，请在连接与权限检查。", flush=True)
        while not stopped.wait(15):
            try:
                with startup_guard(ROOT):
                    if not core_ready(ROOT):
                        ensure_core(ROOT)
                    if activity_allowed(ROOT) and not (lock_held(ROOT / "data/astrbot-onebot/launcher.lock")
                            and port_open(layout.dashboard_port) and port_open(layout.onebot_port)):
                        ensure_bridge(ROOT)
            except Exception:
                print("本机服务暂未就绪，将稍后检查；未停止其他程序。", flush=True)
        # Core/bridge are detached owned processes. Stopping the supervisor alone
        # must not change the user's QQ reply switch or discard the QQ session.


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("XZBot 后台启动未完成，请检查本机安装与端口。", flush=True)
        raise SystemExit(1) from None
