#!/usr/bin/env python3
"""Explicit, guarded QQ cutover; never changes the existing DSH installation."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / "src"))

import httpx
import yaml
from azusa_bot.secrets import load_credentials
from azusa_bot.settings import private_write, read_settings, write_settings
from azusa_bot.runtime_layout import runtime_layout

ASTRBOT_PIN = "61335414433db721b3f7ecc93f90deafe0a59d78"
ASTRBOT_VERSION = "4.29.4"
BRIDGE_VERSION = "0.1.0"


class CutoverError(Exception):
    """Safe operator-facing error; never contains provider responses or secrets."""


def process_snapshot() -> list[str]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,comm="],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CutoverError("无法检查旧 DSH 进程；请在 Mac 的正常终端运行此脚本。") from None
    if result.returncode or not result.stdout.strip():
        raise CutoverError("无法可靠检查旧 DSH 进程，已拒绝启动。")
    commands = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            raise CutoverError("进程检查结果无法解析，已拒绝启动。")
        commands.append(parts[1])
    return commands


def check_old_dsh_stopped(home: Path, asserted: bool) -> None:
    if not asserted:
        raise CutoverError("先停止旧 DSH 的 QQ 接入，再使用 --old-dsh-stopped 明确确认；脚本不会停止或修改 DSH。")
    patch = home / ".dsh/profiles/desktop/cordis.patch.yml"
    config_disabled = False
    if patch.exists():
        try:
            data = yaml.safe_load(patch.read_text())
            if not isinstance(data, list):
                raise ValueError()
            entries = [entry for entry in data if isinstance(entry, dict) and entry.get("id") == "im-qqbot"]
            # A missing entry is not proof: the base configuration may still load it.
            config_disabled = bool(entries) and all(entry.get("disabled") is True for entry in entries)
        except (OSError, ValueError, yaml.YAMLError):
            raise CutoverError("无法可靠读取旧 QQ 插件启用状态，已拒绝启动。") from None
    if config_disabled:
        return
    commands = process_snapshot()
    main_running = any(Path(command).name == "DeepSeek Harness" for command in commands)
    if main_running:
        raise CutoverError("旧 DeepSeek Harness 主程序仍在运行，且 QQ 插件未明确禁用。请先用“退出”完全关闭旧程序，再重试。")


def port_listening(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            code = probe.connect_ex(("127.0.0.1", port))
    except OSError:
        raise CutoverError("无法检查本机端口，已拒绝启动。") from None
    # ECONNREFUSED means no listener; permission/network errors are not absence.
    import errno
    if code == 0:
        return True
    if code == errno.ECONNREFUSED:
        return False
    raise CutoverError("本机端口检查结果不明确，已拒绝启动。")


def verify_versions(root: Path) -> Path:
    try:
        source = Path((root / "data/astrbot-source.path").read_text().strip())
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        if result.returncode or result.stdout.strip() != ASTRBOT_PIN:
            raise CutoverError("AstrBot 源码版本与已验证版本不一致，请重新运行 setup_astrbot.sh。")
        dirty = subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"], capture_output=True, timeout=5)
        if dirty.returncode:
            raise CutoverError("AstrBot 源码存在未验证修改，已拒绝启动。")
        version = subprocess.run(
            [str(source / ".venv/bin/python"), "-c", "import importlib.metadata; print(importlib.metadata.version('astrbot'))"],
            capture_output=True, text=True, timeout=10,
        )
        if version.returncode or version.stdout.strip() != ASTRBOT_VERSION:
            raise CutoverError("AstrBot 运行环境版本不匹配，已拒绝启动。")
        metadata = yaml.safe_load((root / "astrbot_plugin_azusa_bridge/metadata.yaml").read_text())
        if str(metadata.get("version")) != BRIDGE_VERSION or metadata.get("astrbot_version") != ">=4.29.4,<4.30":
            raise CutoverError("QQ 桥接插件版本与已验证版本不一致。")
        installed = root / "data/astrbot/data/plugins/astrbot_plugin_azusa_bridge"
        if installed.resolve(strict=True) != (root / "astrbot_plugin_azusa_bridge").resolve(strict=True):
            raise CutoverError("AstrBot 中安装的桥接不是当前项目版本，请重新运行 setup_astrbot.sh。")
        return source
    except CutoverError:
        raise
    except (OSError, ValueError, AttributeError, yaml.YAMLError, subprocess.TimeoutExpired):
        raise CutoverError("无法验证 AstrBot 或桥接插件安装，请先运行 setup_astrbot.sh。") from None


def validate_bindings(settings, credentials) -> None:
    if not settings.app_id or not credentials.qq_app_id or not credentials.qq_secret:
        raise CutoverError("QQ 凭据或 AppID 未准备好；请使用现有安全配置或环境变量，不要粘贴到聊天中。")
    if settings.app_id != credentials.qq_app_id:
        raise CutoverError("QQ AppID 与本项目绑定不一致，已拒绝启动。")
    if not settings.owners or not (settings.allowed_groups or settings.allowed_dm_users):
        raise CutoverError("请先在管理界面绑定主人并设置白名单。")
    for owner in settings.owners:
        valid = (
            owner.kind == "group" and owner.scope_id in settings.allowed_groups
        ) or (
            owner.kind == "dm" and owner.scope_id == owner.user_id and owner.user_id in settings.allowed_dm_users
        )
        if not valid:
            raise CutoverError("有主人身份未包含在对应群/私聊白名单，已拒绝启动。")
    if settings.provider_mode != "cloud" or not credentials.deepseek_key:
        raise CutoverError("QQ 实际接入需要已配置的云端 DeepSeek，不能使用模拟模式。")
    if settings.voice_enabled and (not settings.voice_authorized or not credentials.fish_key or not credentials.fish_reference):
        raise CutoverError("语音已启用，但音色授权或 Fish 配置未完整，请先在本机管理页完成设置。")


def prepare(root: Path, asserted: bool):
    layout = runtime_layout(root)
    check_old_dsh_stopped(Path.home(), asserted)
    if port_listening(layout.core_port - 1):
        raise CutoverError(f"{layout.core_port - 1} 上已有独立 AstrBot 实例，请先停止该测试实例；脚本不会强行结束任何程序。")
    verify_versions(root)
    settings = read_settings(root)
    credentials = load_credentials(settings.read_existing_dsh)
    validate_bindings(settings, credentials)
    try:
        for name in ("admin.token", "bridge.token"):
            path = root / "data" / name
            if stat.S_IMODE(path.stat().st_mode) & 0o077 or len(path.read_text().strip()) < 32:
                raise ValueError()
        with httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client:
            response = client.get(layout.core_url + "/health")
            if response.status_code != 200 or response.json().get("ok") is not True:
                raise ValueError()
            response = client.get(layout.core_url + "/admin/settings", headers={"Authorization": "Bearer " + (root / "data/admin.token").read_text().strip()})
            if response.status_code != 200 or response.json().get("app_id") != settings.app_id:
                raise ValueError()
    except (httpx.HTTPError, ValueError, AttributeError, OSError):
        raise CutoverError(f"Azusa 核心服务尚未在本机 {layout.core_port} 就绪，请先运行 scripts/run.sh。") from None
    bridge_path = root / "data/astrbot/data/config/astrbot_plugin_azusa_bridge_config.json"
    try:
        original_bridge = bridge_path.read_text(encoding="utf-8-sig")
        bridge = json.loads(original_bridge)
        if not isinstance(bridge, dict):
            raise ValueError()
    except (OSError, ValueError):
        raise CutoverError("桥接配置缺失或损坏，请先运行 setup_astrbot.sh。") from None
    return settings, credentials, bridge_path, original_bridge, bridge


def main(argv=None):
    parser = argparse.ArgumentParser(description="安全切换到新的私人 QQ Bot；不会修改或关闭 DSH。")
    parser.add_argument("--old-dsh-stopped", action="store_true", help="确认旧 DSH QQ 接入已停止")
    parser.add_argument("--check", action="store_true", help="只检查，不写配置、不连接 QQ")
    parser.add_argument("--disable", action="store_true", help="停止新 AstrBot 后关闭本项目 QQ 开关，便于恢复旧 DSH")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.disable:
            if args.old_dsh_stopped or args.check:
                raise CutoverError("--disable 请单独使用。")
            if port_listening(runtime_layout(ROOT).core_port - 1):
                raise CutoverError(f"请先在运行新 AstrBot 的终端按 Ctrl+C，确认 {runtime_layout(ROOT).core_port - 1} 已停止，再关闭配置开关。")
            settings = read_settings(ROOT)
            settings.qq_enabled = False
            write_settings(ROOT, settings)
            path = ROOT / "data/astrbot/data/config/astrbot_plugin_azusa_bridge_config.json"
            config = json.loads(path.read_text(encoding="utf-8-sig"))
            config["enabled"] = False
            private_write(path, json.dumps(config, indent=2) + "\n")
            print("新 Bot 已断开并关闭 QQ 开关；原 DSH 配置未改动，可以重新打开旧 DSH。")
            return 0
        settings, credentials, bridge_path, original_bridge, bridge = prepare(ROOT, args.old_dsh_stopped)
        if args.check:
            print("检查通过；未修改配置，未建立 QQ 连接。")
            return 0
        previous_settings = settings.model_copy(deep=True)
        bridge.update(enabled=True, app_id=settings.app_id, adapter_id="azusa-official", core_url=runtime_layout(ROOT).core_url)
        env = os.environ.copy()
        env.update({
            "AZUSA_ROOT": str(ROOT),
            "AZUSA_QQ_CONNECT": "1", "AZUSA_OLD_QQ_STOPPED": "1",
            "AZUSA_QQ_APP_ID": credentials.qq_app_id, "AZUSA_QQ_APP_SECRET": credentials.qq_secret,
            "AZUSA_BRIDGE_TOKEN_FILE": str(ROOT / "data/bridge.token"),
            "AZUSA_MEDIA_ROOT": str(ROOT / "data/media"),
        })
        try:
            private_write(bridge_path, json.dumps(bridge, indent=2) + "\n")
            settings.qq_enabled = True
            write_settings(ROOT, settings)
            # Environment only: credentials never appear in argv, files or output.
            os.execve(str(ROOT / "scripts/run_astrbot.sh"), [str(ROOT / "scripts/run_astrbot.sh")], env)
        except OSError:
            write_settings(ROOT, previous_settings)
            private_write(bridge_path, original_bridge)
            raise CutoverError("启动失败，已恢复本项目原有开关；旧 DSH 未改动。") from None
    except CutoverError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("本地配置检查失败，未建立 QQ 连接；请检查本项目配置文件。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
