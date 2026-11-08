"""Fixed loopback ports and paths for a bounded set of owned QQ instances.

The manager writes an immutable instance manifest during creation. Browser
settings cannot supply arbitrary ports, hosts, commands or external directories.
The original project keeps every legacy port and runtime path.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path
    manager_root: Path
    slot: int = 0

    @property
    def is_managed(self):
        return self.slot != 0

    @property
    def core_port(self):
        return 6196 + 20 * self.slot

    @property
    def dashboard_port(self):
        return self.core_port + 1

    @property
    def onebot_port(self):
        return self.core_port + 3

    @property
    def napcat_port(self):
        return self.core_port + 4

    @property
    def gemini_port(self):
        return self.core_port + 5

    @property
    def core_url(self):
        return f"http://127.0.0.1:{self.core_port}"

    @property
    def napcat_url(self):
        return f"http://127.0.0.1:{self.napcat_port}"

    @property
    def gemini_url(self):
        return f"http://127.0.0.1:{self.gemini_port}/openai/v1"

    @property
    def lima_home(self):
        if self.is_managed:
            return self.manager_root / "runtime/account-vms" / self.root.name[:12]
        return (self.root / "runtime/lima").resolve()


def runtime_layout(root: Path) -> RuntimeLayout:
    root = Path(root).absolute()
    manifest = root / "config/instance.json"
    if not manifest.exists():
        # A managed child must never silently fall back onto primary ports.
        if root.parent.name == "accounts" and re.fullmatch(r"[a-f0-9]{32}", root.name):
            raise ValueError("runtime_instance_manifest_missing")
        return RuntimeLayout(root.resolve(), root.resolve())
    try:
        if root.is_symlink() or root.parent.is_symlink() or (root / "config").is_symlink():
            raise ValueError()
        descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 4096 or info.st_mode & 0o077:
                raise ValueError()
            data = json.load(stream)
        if (not isinstance(data, dict) or set(data) != {"schema", "slot", "manager_root"}
                or type(data["schema"]) is not int or data["schema"] != 1
                or type(data["slot"]) is not int or not 1 <= data["slot"] <= 3
                or not isinstance(data["manager_root"], str)):
            raise ValueError()
        manager = Path(data["manager_root"])
        if (not manager.is_absolute() or not manager.is_dir() or manager.is_symlink()
                or manager.resolve() != manager or not re.fullmatch(r"[a-f0-9]{32}", root.name)
                or root.resolve() != manager / "accounts" / root.name):
            raise ValueError()
        return RuntimeLayout(root.resolve(), manager, data["slot"])
    except (OSError, ValueError, TypeError):
        raise ValueError("runtime_instance_manifest_invalid") from None
