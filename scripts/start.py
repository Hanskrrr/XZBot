"""Open the authenticated local management page; never start or connect QQ."""
from __future__ import annotations

import os
import subprocess

from start_onebot import ROOT, ensure_core, open_management, startup_guard


def main():
    os.umask(0o077)
    (ROOT / "data").mkdir(exist_ok=True, mode=0o700)
    with startup_guard(ROOT):
        ensure_core(ROOT)
    open_management(ROOT)
    print("QQ 保持原状态。")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except (OSError, subprocess.SubprocessError):
        raise SystemExit("管理页打开未完成。请重新双击「启动管理台.command」；没有修改 QQ 接入。") from None
