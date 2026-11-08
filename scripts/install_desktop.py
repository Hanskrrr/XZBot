"""Install XZBot, preserving the project's old launcher as a compatibility alias."""
from pathlib import Path
import os
import plistlib
import shlex

ROOT = Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[1])).resolve()


def install():
    target = Path.home() / "Applications/XZBot.app"
    previous = Path.home() / "Applications/QQBot.app"
    old_marker = previous / "Contents/Resources/qqbot-project.txt"
    owned_previous = previous.is_dir() and old_marker.is_file() and old_marker.read_text().strip() == str(ROOT)
    if not target.exists() and not target.is_symlink() and owned_previous and not previous.is_symlink():
        previous.rename(target)
    marker = target / "Contents/Resources/qqbot-project.txt"
    if target.is_symlink() or (target.exists() and (not marker.is_file() or marker.read_text().strip() != str(ROOT))):
        raise RuntimeError("XZBot.app 已存在且不属于此项目；未覆盖。")
    (target / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(ROOT) + "\n")
    data = {"CFBundleName": "XZBot", "CFBundleDisplayName": "XZBot", "CFBundleIdentifier": "local.qqbot.desktop",
            "CFBundleVersion": "2", "CFBundleShortVersionString": "0.3.0", "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "XZBot", "LSUIElement": True, "NSHighResolutionCapable": True}
    (target / "Contents/Info.plist").write_bytes(plistlib.dumps(data))
    executable = target / "Contents/MacOS/XZBot"
    executable.write_text("#!/bin/sh\nexec " + shlex.quote(str(ROOT / ".venv/bin/python")) + " " +
                          shlex.quote(str(ROOT / "scripts/desktop_launcher.py")) + " >/dev/null 2>&1\n")
    executable.chmod(0o755)
    legacy_link = ROOT / "QQBot.app"
    if legacy_link.is_symlink() and legacy_link.readlink() == previous:
        legacy_link.unlink()
        legacy_link.symlink_to(target, target_is_directory=True)
    link = ROOT / "XZBot.app"
    if link.is_symlink() and link.resolve() == target:
        return target
    if link.exists() or link.is_symlink():
        raise RuntimeError("项目中的 XZBot.app 已存在；未覆盖。")
    link.symlink_to(target, target_is_directory=True)
    return target


if __name__ == "__main__":
    print("XZBot 应用入口已安装：" + str(install()))
