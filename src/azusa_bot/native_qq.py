"""Operate only an isolated, already prepared macOS QQ bundle owned by XZBot."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time

from .runtime_layout import runtime_layout
from .settings import private_write


def directory(root):
    return root / 'runtime/native-napcat'


def executable(root):
    return directory(root) / 'QQ.app/Contents/MacOS/QQ'


def prepared(root):
    base = directory(root)
    try:
        marker = base / 'prepared.json'
        return (not base.is_symlink() and not marker.is_symlink()
                and json.loads(marker.read_text()) == {'schema': 1, 'root': str(root.resolve())}
                and executable(root).is_file() and (base/'napcat/napcat.mjs').is_file())
    except (OSError, ValueError):
        return False


def _identity(pid):
    if type(pid) is not int or pid <= 1:
        raise RuntimeError('runtime_ownership_unverified')
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


def _record(root):
    path = directory(root) / 'process.json'
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 4096:
        raise RuntimeError('runtime_ownership_unverified')
    data = json.loads(path.read_text())
    actual = _identity(data.get('pid'))
    if actual is None:
        return None
    if actual != data.get('identity') or str(executable(root)) not in actual:
        raise RuntimeError('runtime_ownership_unverified')
    return data


def start(root):
    if not prepared(root):
        raise RuntimeError('runtime_not_prepared')
    if _record(root):
        return
    from .host import require_free_loopback_port
    require_free_loopback_port(runtime_layout(root).napcat_port)
    base = directory(root)
    # Never change HOME or fall back to the daily QQ data directory.
    env = {**os.environ, 'NAPCAT_QQ_DATA_DIR': str(base/'qq-data'),
           'NAPCAT_WORKDIR': str(base/'napcat-work')}
    process = subprocess.Popen([str(executable(root)), '--no-sandbox'], cwd=base, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    identity = _identity(process.pid)
    if not identity or str(executable(root)) not in identity:
        raise RuntimeError('runtime_start_failed')
    private_write(base/'process.json', json.dumps({'pid':process.pid, 'identity':identity}))


def stop(root):
    data = _record(root)
    if not data:
        return
    if os.getpgid(data['pid']) != data['pid']:
        raise RuntimeError('runtime_ownership_unverified')
    os.killpg(data['pid'], signal.SIGTERM)
    for _ in range(100):
        if _identity(data['pid']) is None:
            return
        time.sleep(.1)
    raise RuntimeError('runtime_stop_timeout')
