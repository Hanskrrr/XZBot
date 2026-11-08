"""Disable replies, then stop only the project's isolated QQ VM; leave management open."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / "src"))
from azusa_bot.settings import private_write
from start_onebot import spawn


def disable_replies(root: Path):
    path = root / "config/local.json"
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("没有找到本项目的有效配置，停止入口未执行其他操作。")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError("项目配置格式异常，请先在本机管理页检查。")
    data["onebot_enabled"] = False
    private_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def owned_vm(root: Path):
    try:
        directory = (root / "runtime/lima").resolve(strict=True)
        return ((directory / ".azusa-project").read_text().strip() == str(root)
                and (directory / "azusa/lima.yaml").is_file())
    except OSError:
        return False


def main():
    os.umask(0o077)
    disable_replies(ROOT)
    print("普通 QQ 回复已关闭。", flush=True)
    if not owned_vm(ROOT):
        print("没有找到属于本项目的隔离 QQ 虚拟机，无需停止。")
        return
    child = spawn(ROOT, [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/napcat_runtime.py"), "shutdown"], "napcat-stop.log")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = child.poll()
        if status is not None:
            if status:
                print("回复已关闭，但隔离环境停止未完成；请检查 data/napcat-stop.log。")
            else:
                print("本项目的隔离 QQ 环境已停止。")
            break
        time.sleep(.5)
    else:
        print("回复已关闭，隔离环境仍在停止；不会强制终止其他程序。")
    print("本机管理页与空闲桥接保留，可继续检查记录。DSH 和桌面 QQ 未修改。")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("停止入口未完成，请检查本项目配置与 data/napcat-stop.log。") from None
