"""Launch the pinned transport; QQ credentials exist only in process memory."""

import copy
import errno
import fcntl
import os
import runpy
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])) / "src"))
from azusa_bot.runtime_layout import runtime_layout
layout = runtime_layout(Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])))
os.environ["DASHBOARD_PORT"] = str(layout.core_port - 1)
source = Path(os.environ["AZUSA_ASTRBOT_SOURCE"]).resolve()
sys.path.insert(0, str(source))

# Hold the process lock through runpy/main to prevent simultaneous launchers
# from both connecting to QQ before the dashboard eventually binds its port.
lock_path = Path(os.environ["ASTRBOT_ROOT"]) / "launcher.lock"
lock_handle = lock_path.open("a")
try:
    fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("已有本项目 AstrBot 进程运行，已拒绝重复启动。") from None
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        port_status = probe.connect_ex(("127.0.0.1", layout.core_port - 1))
except OSError:
    raise SystemExit(f"无法检查 {layout.core_port - 1} 端口，未连接 QQ。") from None
if port_status != errno.ECONNREFUSED:
    raise SystemExit(f"{layout.core_port - 1} 端口已占用或无法检查，未连接 QQ。")

from astrbot.core.platform.manager import PlatformManager

original_load = PlatformManager.load_platform


async def guarded_load(manager, config):
    # The saved configuration always remains disconnected and has no secret.
    if config.get("type") != "qq_official" or config.get("id") != "azusa-official":
        return
    if os.environ.get("AZUSA_QQ_CONNECT") != "1":
        return
    if os.environ.get("AZUSA_OLD_QQ_STOPPED") != "1":
        raise RuntimeError("Stop the existing DSH QQ adapter before live cutover")
    app_id = os.environ.get("AZUSA_QQ_APP_ID", "")
    secret = os.environ.get("AZUSA_QQ_APP_SECRET", "")
    if not app_id or not secret:
        raise RuntimeError("QQ credentials are missing from the process environment")
    ephemeral = copy.deepcopy(config)
    ephemeral.update(enable=True, appid=app_id, secret=secret)
    try:
        await original_load(manager, ephemeral)
    finally:
        # The adapter copies secret into its private authentication field in
        # __init__; remove it from its inspectable config dictionary afterward.
        ephemeral["secret"] = ""


PlatformManager.load_platform = guarded_load
sys.argv = [str(source / "main.py")]
runpy.run_path(str(source / "main.py"), run_name="__main__")
