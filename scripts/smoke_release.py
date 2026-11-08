"""CI-only launch validation. Refuse occupied ports and any enabled QQ account."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
from azusa_bot.host import python_at
from azusa_bot.settings import read_settings,token


def available(port):
    with socket.socket() as s:return s.connect_ex(('127.0.0.1',port))!=0


def main():
    if os.environ.get('CI')!='true':raise RuntimeError('This disposable smoke test runs only on CI.')
    if read_settings(ROOT).onebot_enabled:raise RuntimeError('Refusing an enabled QQ account.')
    if not all(available(p) for p in (6196,6197,6199)):raise RuntimeError('Refusing occupied ports.')
    environment={**os.environ,'AZUSA_ROOT':str(ROOT),'PYTHONPATH':str(ROOT/'src'),'PYTHONUTF8':'1'}
    source=Path((ROOT/'data/astrbot-source.path').read_text().strip())
    children=[]
    with (ROOT/'data/release-smoke.log').open('wb') as log:
        try:
            children.append(subprocess.Popen([sys.executable,'-m','azusa_bot.cli','serve'],env=environment,stdout=log,stderr=log))
            children.append(subprocess.Popen([str(python_at(source/'.venv')),str(ROOT/'scripts/run_onebot_astrbot.py')],env=environment,stdout=log,stderr=log))
            deadline=time.monotonic()+150
            while time.monotonic()<deadline:
                if any(c.poll() is not None for c in children):raise RuntimeError('A native service exited before readiness.')
                if all(not available(p) for p in (6196,6197,6199)):break
                time.sleep(.5)
            else:raise RuntimeError('Native services did not become ready.')
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request=urllib.request.Request('http://127.0.0.1:6196/admin/status',headers={'Authorization':'Bearer '+token(ROOT,'admin')})
            with opener.open(request,timeout=5) as response:assert json.load(response)['settings']['onebot_enabled'] is False
            # An open event port must still reject unauthenticated HTTP events.
            try:opener.open('http://127.0.0.1:6199/',timeout=5)
            except urllib.error.HTTPError as error:assert error.code==403
            else:raise RuntimeError('Unauthenticated event endpoint was exposed.')
            print('Native core, authenticated management and fixed AstrBot bridge ready. QQ remains disconnected.')
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=15)
                    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)


if __name__=='__main__':main()
