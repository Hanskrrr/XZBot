"""Manage an isolated QQ runtime; never touches the Mac QQ app or DSH config."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / "src"))
from azusa_bot.runtime_layout import runtime_layout

LAYOUT = runtime_layout(ROOT)
VM = "azusa"
VERSION = "4.18.42"
APP = "QQ-53644_NapCat-v4.18.42-arm64.AppImage"
URL = "https://github.com/NapNeko/NapCatAppImageBuild/releases/download/v4.18.42/" + APP
SHA256 = "cf53d381db4452eed35f795b7bf5d6b592839146306781ec5cf342a380e5acd3"
LIMA_DIR = (LAYOUT.lima_home if LAYOUT.is_managed else
            Path(os.environ.get("AZUSA_LIMA_HOME", str(LAYOUT.lima_home))).resolve())


def environment():
    env = os.environ.copy()
    env["LIMA_HOME"] = str(LIMA_DIR)
    return env


def lima(*args, input=None, capture=False):
    binary = shutil.which("limactl") or "/opt/homebrew/bin/limactl"
    return subprocess.run([binary, "--tty=false", *args], env=environment(),
                          input=input, text=True, check=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def ensure_private_token(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(40) + "\n")
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError("令牌文件必须仅当前用户可读写。")
    value = path.read_text().strip()
    if len(value) < 32:
        raise RuntimeError("访问令牌长度不足。")
    return value


def prepare_paths():
    # Stable short path avoids macOS UNIX socket limits; never uses ~/.lima.
    pointer = ROOT / "runtime/lima"
    marker = LIMA_DIR / ".azusa-project"
    if LIMA_DIR.exists() and (not marker.exists() or marker.read_text().strip() != str(ROOT)):
        raise RuntimeError("独立虚拟机目录已存在且不属于此项目；拒绝覆盖。")
    LIMA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.write_text(str(ROOT) + "\n")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    if pointer.absolute() != LIMA_DIR.absolute() and not pointer.exists():
        pointer.symlink_to(LIMA_DIR, target_is_directory=True)
    if pointer.resolve() != LIMA_DIR:
        raise RuntimeError("虚拟机目录指向不匹配。")


def download():
    target = LAYOUT.manager_root / "runtime/downloads" / APP
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(".partial")
        with urllib.request.urlopen(URL, timeout=60) as response, temporary.open("wb") as f:
            shutil.copyfileobj(response, f)
        temporary.replace(target)
    with target.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    if digest != SHA256:
        raise RuntimeError("NapCat 官方发布文件校验失败；不会执行。")
    print("NapCat 官方 ARM64 文件 SHA256 校验通过。", flush=True)
    return target


def configure():
    web_token = ensure_private_token(ROOT / "data/napcat-webui.token")
    ws_token = ensure_private_token(ROOT / "data/onebot.token")
    payload = json.dumps({
        "webui.json": {"host": "127.0.0.1", "port": 6099, "token": web_token,
                       "autoLoginAccount": "", "disableWebUI": False},
        "napcat.json": {"fileLog": False, "consoleLog": False, "fileLogLevel": "error", "consoleLogLevel": "error"},
        "onebot11.json": {"network": {"websocketClients": [{
            "name": "azusa-local", "enable": True,
            "url": f"ws://host.lima.internal:{LAYOUT.onebot_port}/ws", "token": ws_token,
            "messagePostFormat": "array", "reportSelfMessage": False, "debug": False,
            "heartInterval": 30000, "reconnectInterval": 5000}]}}
    })
    # Secrets go to SSH stdin, never command arguments or console/logs.
    writer = "import sys,json,pathlib,os; os.umask(0o077); p=pathlib.Path('/opt/azusa/napcat/config'); p.mkdir(parents=True,exist_ok=True); data=json.load(sys.stdin); [(p/n).write_text(json.dumps(v)) for n,v in data.items()]"
    lima("shell", VM, "python3", "-c", writer, input=payload, capture=True)
    unit = """[Unit]
Description=Isolated Azusa NapCat (manual start)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=azusa
Group=azusa
WorkingDirectory=/opt/azusa/napcat
Environment=NAPCAT_QQ_DATA_DIR=/opt/azusa/qq-data
UMask=0077
ExecStart=/opt/azusa/NapCat.AppImage --appimage-extract-and-run
Restart=no
StandardOutput=null
StandardError=null
KillMode=control-group
[Install]
WantedBy=multi-user.target
"""
    lima("shell", VM, "sudo", "tee", "/etc/systemd/system/azusa-napcat.service", input=unit, capture=True)
    lima("shell", VM, "sudo", "systemctl", "daemon-reload", capture=True)


def vm_template():
    """Materialize only this instance's fixed port mapping; no VM is started."""
    template = ROOT / "scripts/napcat-lima.yaml"
    if not LAYOUT.is_managed:
        return template
    content = template.read_text()
    if content.count("hostPort: 6200") != 1:
        raise RuntimeError("虚拟机模板不匹配；拒绝自动修改。")
    destination = ROOT / "runtime/napcat-lima.yaml"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(content.replace("hostPort: 6200", f"hostPort: {LAYOUT.napcat_port}"))
    destination.chmod(0o600)
    return destination


def setup():
    prepare_paths()
    artifact = download()
    instance = LIMA_DIR / VM / "lima.yaml"
    lima("start", VM if instance.exists() else str(vm_template()),
         "--name=" + VM, "--timeout=15m")
    lima("copy", str(artifact), VM + ":/opt/azusa/NapCat.AppImage", capture=True)
    lima("shell", VM, "chmod", "700", "/opt/azusa/NapCat.AppImage", capture=True)
    configure()
    (ROOT / "runtime/napcat-prepared.json").write_text(json.dumps({
        "version": VERSION, "sha256": SHA256, "lima_home": str(LIMA_DIR),
        "project_root": str(ROOT), "vm": VM,
    }, indent=2) + "\n")
    print("隔离环境已准备；未登录 QQ，未启用开机自启。")


def verify_prepared():
    marker = ROOT / "runtime/napcat-prepared.json"
    if not marker.is_file():
        raise RuntimeError("隔离环境尚未完成准备，请先运行 setup。")
    data = json.loads(marker.read_text())
    expected = {"version": VERSION, "sha256": SHA256,
                "lima_home": str(LIMA_DIR), "project_root": str(ROOT), "vm": VM}
    if data != expected:
        raise RuntimeError("隔离运行版本或项目路径不匹配；拒绝启动。")


def wait_ready():
    # Local management requests must never pass through a configured HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(45):
        try:
            with opener.open(LAYOUT.napcat_url + "/", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("NapCat 管理页面未就绪；账号未自动登录，请检查隔离运行状态。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "setup", "start", "stop", "shutdown", "status"])
    action = parser.parse_args().action
    if action == "download":
        download()
        return
    if action == "setup":
        setup()
        return
    prepare_paths()
    if action == "start":
        verify_prepared()
        lima("start", VM)
        lima("shell", VM, "sudo", "systemctl", "start", "azusa-napcat", capture=True)
        wait_ready()
        print("NapCat 已启动；登录请使用本机管理页面。")
    elif action == "stop":
        lima("shell", VM, "sudo", "systemctl", "stop", "azusa-napcat", capture=True)
        print("NapCat 已停止；桌面 QQ 与 DSH 未修改。")
    elif action == "shutdown":
        lima("stop", VM)
    elif action == "status":
        lima("list")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit("隔离环境操作失败。请检查虚拟机状态；未显示可能含会话信息的底层输出。") from None
    except (OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
