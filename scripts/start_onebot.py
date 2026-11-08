"""Double-click launcher: reuse local services, start only a prepared isolated QQ VM."""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / "src"))
from azusa_bot.settings import private_write, token
from azusa_bot.bot_control import activity_allowed
from azusa_bot.runtime_layout import runtime_layout
from napcat_runtime import VERSION as NAPCAT_VERSION, SHA256 as NAPCAT_SHA256, VM as NAPCAT_VM

CORE = runtime_layout(ROOT).core_url


def port_open(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(.35)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def core_ready(root: Path) -> bool:
    """Authenticate the existing service instead of trusting an occupied port."""
    path = root / "data/admin.token"
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        return False
    try:
        request = urllib.request.Request(runtime_layout(root).core_url + "/admin/status",
            headers={"Authorization": "Bearer " + path.read_text().strip()})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=1) as response:
            data = json.load(response)
        return (isinstance(data, dict) and bool(data.get("version")) and "settings" in data
                and data.get("project_path") == str(root.resolve()))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def lock_held(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


@contextmanager
def startup_guard(root: Path):
    """All GUI, supervisor and legacy script starts share this interprocess lock."""
    (root / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / "data/start-onebot.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def private_log(root: Path, name: str):
    path = root / "data" / name
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "ab", buffering=0)


def spawn(root: Path, arguments: list[str], logfile: str):
    environment = os.environ.copy()
    environment.update(AZUSA_ROOT=str(root), PYTHONPATH=str(root / "src"))
    with private_log(root, logfile) as output:
        return subprocess.Popen(arguments, cwd=root, env=environment,
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)


def wait_ready(check, child=None, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        if child is not None and child.poll() is not None:
            return False
        time.sleep(.25)
    return check()


def ensure_core(root: Path):
    if core_ready(root):
        print("本机管理服务已运行，继续使用。", flush=True)
        return
    if port_open(runtime_layout(root).core_port):
        raise RuntimeError("管理端口已被其他服务占用；没有停止任何程序。请检查本机管理服务。")
    child = spawn(root, [str(root / ".venv/bin/python"), "-m", "azusa_bot.cli", "serve"], "core.log")
    private_write(root / "data/core.pid", str(child.pid))
    if not wait_ready(lambda: core_ready(root), child):
        raise RuntimeError("管理服务尚未就绪，请检查 data/core.log。没有停止其他程序。")
    print("本机管理服务已启动。", flush=True)


def ensure_bridge(root: Path):
    layout = runtime_layout(root)
    lock = root / "data/astrbot-onebot/launcher.lock"
    ready = lambda: lock_held(lock) and port_open(layout.dashboard_port) and port_open(layout.onebot_port)
    if ready():
        print("普通 QQ 桥接已运行，继续使用。", flush=True)
        return
    if lock_held(lock):
        if wait_ready(ready):
            print("普通 QQ 桥接已就绪。", flush=True)
            return
        raise RuntimeError("已有普通 QQ 桥接仍在启动，请检查 data/onebot-launch.log；不会自动重启。")
    if port_open(layout.dashboard_port) or port_open(layout.onebot_port):
        raise RuntimeError("普通 QQ 桥接端口已被占用；没有停止其他程序。")
    source_file = root / "data/astrbot-source.path"
    if not source_file.is_file():
        raise RuntimeError("桥接环境尚未准备，请先完成项目的安装步骤。")
    source = Path(source_file.read_text().strip())
    if not (source / ".venv/bin/python").is_file():
        raise RuntimeError("桥接运行环境不可用，请检查项目安装。")
    child = spawn(root, ["/bin/sh", str(root / "scripts/run_onebot_astrbot.sh")], "onebot-launch.log")
    private_write(root / "data/onebot-launch.pid", str(child.pid))
    if not wait_ready(ready, child, timeout=45):
        raise RuntimeError("普通 QQ 桥接尚未就绪，请检查 data/onebot-launch.log；不会自动重启。")
    print("普通 QQ 桥接已启动。", flush=True)


def vm_prepared(root: Path) -> bool:
    """Never provision/download a VM from a double-click convenience action."""
    pointer = root / "runtime/lima"
    prepared = root / "runtime/napcat-prepared.json"
    try:
        directory = pointer.resolve(strict=True)
        marker = directory / ".azusa-project"
        data = json.loads(prepared.read_text())
        return (marker.is_file() and marker.read_text().strip() == str(root)
                and (directory / NAPCAT_VM / "lima.yaml").is_file()
                and isinstance(data, dict) and data.get("version") == NAPCAT_VERSION
                and data.get("sha256") == NAPCAT_SHA256 and data.get("vm") == NAPCAT_VM
                and data.get("project_root") == str(root) and data.get("lima_home") == str(directory))
    except (OSError, ValueError):
        return False


def open_management(root: Path):
    value = token(root, "admin")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", value):
        raise RuntimeError("本机管理口令格式异常，请检查安全配置。")
    # Pass the private URL through stdin, not process arguments or terminal text.
    try:
        result = subprocess.run(["/usr/bin/osascript", "-"],
            input='open location "' + runtime_layout(root).core_url + '/#token=' + value + '"\n',
            text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("管理服务已就绪，但浏览器未能打开管理页。请重新打开 XZBot 应用，它会安全连接本机管理页。") from None
    if result.returncode:
        raise RuntimeError("管理服务已就绪，但浏览器未能打开管理页。请重新打开 XZBot 应用，它会安全连接本机管理页。")
    print("管理页已打开，将自动连接本机服务。", flush=True)


def start_vm(root: Path):
    if not vm_prepared(root):
        print("隔离 QQ 环境尚未准备完成。已打开管理页；此入口不会下载或安装虚拟机。", flush=True)
        return
    print("正在启动已准备的隔离 QQ 环境…", flush=True)
    child = spawn(root, [str(root / ".venv/bin/python"), str(root / "scripts/napcat_runtime.py"), "start"], "napcat-launch.log")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = child.poll()
        if status is not None:
            if status:
                raise RuntimeError("隔离 QQ 启动未完成，请检查 data/napcat-launch.log。管理页仍可使用。")
            print("隔离 QQ 已启动。请在管理页完成登录与账号绑定。", flush=True)
            return
        time.sleep(.5)
    print("隔离 QQ 仍在后台启动，请稍后在管理页查看状态；没有重复启动或终止它。", flush=True)


def main():
    os.umask(0o077)
    (ROOT / "data").mkdir(mode=0o700, exist_ok=True)
    with (ROOT / "data/start-onebot.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("启动入口正在处理，请稍候。")
            return
        ensure_core(ROOT)
        open_management(ROOT)
        if activity_allowed(ROOT):
            ensure_bridge(ROOT)
            start_vm(ROOT)
        else:
            print("Bot 已关闭；管理页仍可用，请在页面选择启动 Bot。", flush=True)
    print("DSH、桌面 QQ 和官方 QQ 接入均未修改。")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except (OSError, subprocess.SubprocessError):
        raise SystemExit("启动未完成，请检查本项目 data 目录中的启动日志；没有停止其他程序。") from None
