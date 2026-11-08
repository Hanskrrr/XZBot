"""Start/stop a selected, preinstalled adapter. Never downloads or installs."""
import os
from pathlib import Path
import sys
import time

ROOT = Path(os.environ.get('AZUSA_ROOT', Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT/'src'))
from azusa_bot.qq_runtime import read, snow_prepared
from azusa_bot.runtime_layout import runtime_layout
from azusa_bot import native_qq


def snow_state():
    import json
    from napcat_runtime import lima, LIMA_DIR
    marker=LIMA_DIR/'.azusa-project'
    if not marker.is_file() or marker.is_symlink() or marker.read_text().strip()!=str(ROOT):
        raise RuntimeError('runtime_ownership_unverified')
    if not (LIMA_DIR/'sl/lima.yaml').is_file():
        return 'absent'
    result=lima('list','--format','json','sl',capture=True)
    rows=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if len(rows)!=1 or rows[0].get('name')!='sl' or rows[0].get('status') not in {'Running','Stopped'}:
        raise RuntimeError('runtime_status_unknown')
    return rows[0]['status']


def assert_other_qq_stopped(root=ROOT):
    """Independent launch entries must not start a second QQ for this account."""
    import json
    import platform
    if platform.system()!='Darwin':return
    from napcat_runtime import lima, LIMA_DIR
    choice=read(root)
    selected=('azusa' if choice['adapter']=='napcat' else 'sl') if choice['environment']=='lima' else None
    for name in ('azusa','sl'):
        if name==selected or not (LIMA_DIR/name/'lima.yaml').is_file():continue
        marker=LIMA_DIR/'.azusa-project'
        if not marker.is_file() or marker.is_symlink() or marker.read_text().strip()!=str(root):
            raise RuntimeError('runtime_ownership_unverified')
        result=lima('list','--format','json',name,capture=True)
        rows=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if len(rows)!=1 or rows[0].get('name')!=name or rows[0].get('status')!='Stopped':
            raise RuntimeError('runtime_other_adapter_running')
    if choice['environment']!='native' and native_qq._record(root):
        raise RuntimeError('runtime_other_adapter_running')


def operate(action):
    from napcat_runtime import lima
    choice=read(ROOT)
    if choice['environment']=='external':
        return
    if action=='stop':
        # Only the selected runtime is touched. Switching requires this to succeed first.
        if choice['environment']=='native':
            native_qq.stop(ROOT)
        elif choice['adapter']=='snowluma' and snow_state()=='Running':
            lima('stop','sl',capture=True)
            if snow_state()!='Stopped':raise RuntimeError('runtime_not_stopped')
        return
    assert_other_qq_stopped()
    if choice['environment']=='native':
        native_qq.start(ROOT)
    elif choice['adapter']=='snowluma':
        if not snow_prepared(ROOT):
            raise RuntimeError('runtime_not_prepared')
        if snow_state()!='Running':
            lima('start','sl',capture=True)
        lima('shell','sl','sudo','systemctl','start','xzbot-snowluma',capture=True)
    else:
        raise RuntimeError('runtime_invalid')
    from start_onebot import port_open
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        if port_open(runtime_layout(ROOT).napcat_port):
            return
        time.sleep(.5)
    raise RuntimeError('runtime_webui_unavailable')


if __name__=='__main__':
    if len(sys.argv)!=2 or sys.argv[1] not in {'start','stop'}:
        raise SystemExit(2)
    operate(sys.argv[1])
