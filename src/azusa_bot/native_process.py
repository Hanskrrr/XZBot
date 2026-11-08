"""Stop only a PID whose full command line identifies this project's bridge."""
import os
from pathlib import Path
import time


def stop_owned_bridge(root):
    import psutil
    path=Path(root)/'data/onebot-launch.pid'
    if not path.exists():return
    if path.is_symlink() or path.stat().st_size>30:raise RuntimeError('bridge_ownership_unverified')
    pid=int(path.read_text().strip())
    if pid<=1:raise RuntimeError('bridge_ownership_unverified')
    try:process=psutil.Process(pid)
    except psutil.NoSuchProcess:return
    expected=os.path.normcase(str(Path(root)/'scripts/run_onebot_astrbot.py'))
    if expected not in [os.path.normcase(x) for x in process.cmdline()]:raise RuntimeError('bridge_ownership_unverified')
    process.terminate()
    try:process.wait(timeout=15)
    except psutil.TimeoutExpired:raise RuntimeError('bridge_stop_timeout') from None


def terminate_helper(process, *, force=False):
    import signal
    if os.name=='nt':process.kill() if force else process.terminate()
    else:os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
