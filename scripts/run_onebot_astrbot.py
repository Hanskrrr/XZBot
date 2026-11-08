"""OneBot receiver only: no official QQ credentials or other adapters are loaded."""
import copy
import os
from pathlib import Path
import runpy
import sys

from prepare_onebot import ROOT, prepare
from azusa_bot.host import file_locks as fcntl, require_free_loopback_port
from azusa_bot.runtime_layout import runtime_layout

layout = runtime_layout(ROOT)

runtime = prepare()
source = Path((ROOT / "data/astrbot-source.path").read_text().strip()).resolve()
os.environ.update(ASTRBOT_ROOT=str(runtime),ASTRBOT_DISABLE_METRICS="1",
                  DASHBOARD_HOST="127.0.0.1",DASHBOARD_PORT=str(layout.dashboard_port),
                  AZUSA_BRIDGE_TOKEN_FILE=str(ROOT / "data/bridge.token"),
                  AZUSA_ONEBOT_POLICY_FILE=str(ROOT / "config/local.json"),
                  AZUSA_MEDIA_ROOT=str(ROOT / "data/media"))
lock = (runtime / "launcher.lock").open("a")
try:
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("普通 QQ 桥接已运行。") from None
for port in (layout.dashboard_port, layout.onebot_port):
    try:
        require_free_loopback_port(port)
    except OSError as error:
        raise SystemExit(f"普通 QQ 桥接端口 {port} 不可用（系统代码 {error.errno}）。") from None
private_token = (ROOT / "data/onebot.token").read_text().strip()
if len(private_token) < 32:
    raise SystemExit("缺少本机 OneBot 访问令牌。")
sys.path.insert(0,str(source))
os.chdir(source)
from astrbot.core.platform.manager import PlatformManager
from onebot_security import install_adapter_guard

install_adapter_guard()

original_load = PlatformManager.load_platform


async def only_onebot(manager, config):
    if config.get("id") != "azusa-onebot" or config.get("type") != "aiocqhttp":
        return
    ephemeral = copy.deepcopy(config)
    ephemeral.update(enable=True,ws_reverse_host="127.0.0.1",ws_reverse_port=layout.onebot_port,ws_reverse_token=private_token)
    try:
        await original_load(manager,ephemeral)
    finally:
        ephemeral["ws_reverse_token"] = ""


PlatformManager.load_platform = only_onebot
sys.argv = [str(source / "main.py")]
runpy.run_path(str(source / "main.py"),run_name="__main__")
