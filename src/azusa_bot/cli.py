from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .settings import root_path, read_settings
from .runtime_layout import runtime_layout


def main():
    parser = argparse.ArgumentParser(description="XZBot private QQ bot")
    parser.add_argument("command",choices=["serve","open","doctor","backup","probe-cloud"])
    parser.add_argument("--voice",action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    root = root_path()
    if args.command == "serve":
        from .host import file_locks as fcntl
        import logging
        import uvicorn
        from .app import create_app
        logging.basicConfig(level=logging.WARNING,format="%(asctime)s %(levelname)s %(name)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.ERROR)
        (root / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
        with (root / "data/core.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("本项目核心已在运行。") from None
            uvicorn.run(create_app(root),host="127.0.0.1",port=runtime_layout(root).core_port,access_log=False,log_level="warning")
    elif args.command == "open":
        # Reuse the double-click entry's stdin-only URL opening. This opens the
        # local management service and never starts AstrBot, NapCat or QQ.
        sys.path.insert(0, str(root / "scripts"))
        from start_onebot import ensure_core, open_management, startup_guard
        try:
            with startup_guard(root):
                ensure_core(root)
            open_management(root)
        except (RuntimeError, OSError) as error:
            text = str(error) if isinstance(error, RuntimeError) else "管理页打开未完成，请重新双击「启动管理台.command」。"
            raise SystemExit(text) from None
    elif args.command == "doctor":
        from .secrets import resolve_credentials
        settings = read_settings(root)
        print(json.dumps({"python":sys.version.split()[0],"provider_mode":settings.provider_mode,
                          "credentials":resolve_credentials(root,settings)[0].status(),
                          "qq_enabled":settings.qq_enabled,"owner_bindings":len(settings.owners),
                          "voice_authorized":settings.voice_authorized},ensure_ascii=False,indent=2))
    elif args.command == "backup":
        from .app import backup_project
        from .profiles import ProfileEngineManager
        engine = ProfileEngineManager(root)
        try:
            print(backup_project(root,engine))
        finally:
            engine.close()
    elif args.command == "probe-cloud":
        asyncio.run(probe(root,args.voice))


async def probe(root: Path, voice: bool):
    from .providers import ProviderError
    settings = read_settings(root)
    settings.provider_mode = "cloud"
    from .core import Engine
    engine = Engine(root)
    provider = engine.providers(settings)
    engine.memory.close(); engine.groups.close()
    result = {}
    try:
        answer = await provider.complete([{"role":"system","content":'只返回JSON {"text":"云端文字连接正常", "mode":"text"}。'}, {"role":"user","content":"测试连接"}])
        result["deepseek"] = "ok" if isinstance(answer.get("text"),str) else "invalid_shape"
    except ProviderError as exc:
        result["deepseek"] = str(exc)
    if voice:
        if not settings.voice_authorized:
            result["fish"] = "authorization_not_confirmed"
        else:
            try:
                p = await provider.synthesize("你好，这是私人角色机器人的语音连接测试。")
                import wave
                with wave.open(str(p)) as wav:
                    result["fish"] = {"status":"ok","seconds":round(wav.getnframes()/wav.getframerate(),2),"sample_rate":wav.getframerate(),"file":p.name}
            except ProviderError as exc:
                result["fish"] = str(exc)
    # Only statuses and a random local filename, never API responses or credentials.
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
