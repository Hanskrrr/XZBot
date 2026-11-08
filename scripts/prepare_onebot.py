"""Prepare a separate, local-only AstrBot OneBot runtime. Never logs into QQ."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / "src"))
from azusa_bot.settings import private_write, read_settings, token
from azusa_bot.host import private_info, private_path, protect, link_directory
from azusa_bot.runtime_layout import runtime_layout


def prepare():
    os.umask(0o077)
    layout = runtime_layout(ROOT)
    runtime = ROOT / "data/astrbot-onebot"
    (runtime / "data/config").mkdir(parents=True, exist_ok=True)
    (runtime / "data/plugins").mkdir(parents=True, exist_ok=True)
    token(ROOT, "onebot")
    token(ROOT, "bridge")
    path = runtime / "data/cmd_config.json"
    if not path.exists():
        password = "Az1" + secrets.token_urlsafe(27)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000).hex()
        config = {
            "dashboard": {"enable":True,"host":"127.0.0.1","port":layout.dashboard_port,"username":"azusa",
                "password":hashlib.md5(password.encode()).hexdigest(),
                "pbkdf2_password":f"pbkdf2_sha256$600000${salt.hex()}${digest}",
                "password_storage_upgraded":True,"password_change_required":False,
                "jwt_secret":secrets.token_urlsafe(48)},
            "platform":[{"id":"azusa-onebot","type":"aiocqhttp","enable":True,
                "ws_reverse_host":"127.0.0.1","ws_reverse_port":layout.onebot_port,"ws_reverse_token":""}],
            "provider_sources":[],"provider":[],"provider_settings":{"enable":False},
            "provider_tts_settings":{"enable":False,"dual_output":False},
            "platform_settings":{"empty_mention_waiting":False,"empty_mention_waiting_need_reply":False},
            "plugin_set":["astrbot_plugin_azusa_onebot"],"admins_id":[],
            "log_level":"WARNING","log_file_enable":False,"trace_enable":False,"disable_metrics":True,
        }
        private_write(path,json.dumps(config,indent=2))
        private_write(ROOT / "data/astrbot-onebot-admin.password",password)
    settings = read_settings(ROOT)
    private_write(runtime / "data/config/astrbot_plugin_azusa_onebot_config.json", json.dumps({
        "enabled":settings.transport == "onebot" and settings.onebot_enabled,
        "adapter_id":"azusa-onebot","self_id":settings.onebot_self_id,
        "core_url":layout.core_url,
    },indent=2))
    link = runtime / "data/plugins/astrbot_plugin_azusa_onebot"
    if not link.exists():
        link_directory(link, ROOT / 'astrbot_plugin_azusa_onebot')
    return runtime


if __name__ == "__main__":
    prepare()
    print("普通 QQ 接入环境已准备；尚未登录 QQ。")
