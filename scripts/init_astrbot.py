"""Prepare a private disconnected AstrBot runtime without printing secrets."""

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

project = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(project / "src"))
from azusa_bot.runtime_layout import runtime_layout
layout = runtime_layout(project)
source = Path(sys.argv[1]).absolute()
runtime = project / "data" / "astrbot"
os.umask(0o077)
(runtime / "data" / "config").mkdir(parents=True, exist_ok=True)
(runtime / "data" / "plugins").mkdir(parents=True, exist_ok=True)
path = runtime / "data" / "cmd_config.json"
if not path.exists():
    password = "Az1" + secrets.token_urlsafe(27)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()
    config = {
        "dashboard": {
            "enable": True, "host": "127.0.0.1", "port": layout.core_port - 1, "username": "azusa",
            "password": hashlib.md5(password.encode()).hexdigest(),
            "pbkdf2_password": f"pbkdf2_sha256$600000${salt.hex()}${digest}",
            "password_storage_upgraded": True, "password_change_required": False,
            "jwt_secret": secrets.token_urlsafe(48),
        },
        "platform": [{
            "id": "azusa-official", "type": "qq_official", "enable": False,
            "appid": "", "secret": "", "enable_group_c2c": True,
            "enable_guild_direct_message": False, "use_markdown": False,
        }],
        "provider_sources": [], "provider": [], "provider_settings": {"enable": False},
        "provider_tts_settings": {"enable": False, "dual_output": False},
        "platform_settings": {"empty_mention_waiting": False, "empty_mention_waiting_need_reply": False},
        "plugin_set": ["astrbot_plugin_azusa_bridge"],
        "admins_id": [], "log_level": "WARNING", "log_file_enable": False, "trace_enable": False,
        "disable_metrics": True,
    }
    path.write_text(json.dumps(config, indent=2))
    (project / "data" / "astrbot-admin.password").write_text(password + "\n")
plugin_config = runtime / "data" / "config" / "astrbot_plugin_azusa_bridge_config.json"
if not plugin_config.exists():
    plugin_config.write_text(json.dumps({"enabled": False, "adapter_id": "azusa-official", "app_id": "", "core_url": layout.core_url}, indent=2))
link = runtime / "data" / "plugins" / "astrbot_plugin_azusa_bridge"
if not link.exists():
    link.symlink_to(project / "astrbot_plugin_azusa_bridge", target_is_directory=True)
(project / "data" / "astrbot-source.path").write_text(str(source) + "\n")
print(f"AstrBot prepared: loopback {layout.core_port - 1}; QQ disabled; private password file created when needed.")
