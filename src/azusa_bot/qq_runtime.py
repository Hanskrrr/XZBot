"""Account-local QQ adapter choice, separate from the OneBot business layer."""
from __future__ import annotations

import json
import platform
from pathlib import Path

from .settings import private_write
from .runtime_layout import runtime_layout


def read(root: Path) -> dict:
    path = root / 'config/qq-runtime.json'
    if path.is_symlink():raise ValueError('qq_runtime_invalid')
    if path.exists():
        if path.is_symlink() or path.stat().st_size > 2048:
            raise ValueError('qq_runtime_invalid')
        data = json.loads(path.read_text())
        validate(data)
        return data
    from .external_runtime import legacy_enabled
    return {'schema': 1, 'environment': 'external' if legacy_enabled(root) else 'lima', 'adapter': 'napcat'}


def validate(data):
    if (not isinstance(data, dict) or set(data) != {'schema', 'environment', 'adapter'}
            or type(data['schema']) is not int or data['schema'] != 1
            or not isinstance(data['environment'],str) or not isinstance(data['adapter'],str)
            or data['environment'] not in {'lima', 'native', 'external'}
            or data['adapter'] not in {'napcat', 'snowluma'}
            or data['environment'] == 'native' and data['adapter'] != 'napcat'):
        raise ValueError('qq_runtime_invalid')


def save(root, data):
    validate(data)
    if data['environment'] in {'native', 'lima'} and platform.system() != 'Darwin':
        raise ValueError('qq_runtime_platform_unsupported')
    private_write(root / 'config/qq-runtime.json', json.dumps(data))


def local_listener_open(port):
    import socket
    with socket.socket() as sock:
        sock.settimeout(.2)
        return sock.connect_ex(('127.0.0.1',port)) == 0


def snow_prepared(root):
    path = root / 'runtime/snowluma-prepared.json'
    try:
        return (not path.is_symlink() and path.stat().st_size < 2048
                and json.loads(path.read_text()) == {'schema': 1, 'root': str(root.resolve()), 'vm': 'sl'})
    except (OSError, ValueError):
        return False


def describe(root):
    data = read(root)
    layout = runtime_layout(root)
    from .native_qq import prepared as native_prepared
    preparations={'lima_napcat':(root/'runtime/napcat-prepared.json').is_file(),
                  'lima_snowluma':snow_prepared(root),
                  'native_napcat':native_prepared(root)}
    return {**data, 'host': platform.system(), 'prepared':preparations, 'webui_url': layout.napcat_url,
            'webui_port': layout.napcat_port, 'onebot_port': layout.onebot_port,
            'status_port': layout.core_port + 6,
            'desktop_url': f'http://127.0.0.1:{layout.core_port + 7}/vnc.html',
            'managed': data['environment'] != 'external'}


def make_admin(root):
    if read(root)['adapter'] == 'snowluma':
        from .snowluma_admin import SnowLumaAdmin
        return SnowLumaAdmin(root)
    from .napcat_admin import NapCatAdmin
    return NapCatAdmin(root)
