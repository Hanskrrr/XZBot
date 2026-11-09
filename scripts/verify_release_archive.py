"""Install and smoke test the exact ZIP in CI or an explicit local sandbox."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--local',action='store_true',help='Verify in a temporary directory; requires unused ports 6196/6197/6199.')
    args=parser.parse_args()
    if os.environ.get('CI')!='true' and not args.local:raise RuntimeError('Use --local for an explicit disposable installation test.')
    for port in (6196,6197,6199):
        with socket.socket() as connection:
            if connection.connect_ex(('127.0.0.1',port))==0:raise RuntimeError('Verification requires unused ports 6196/6197/6199; no services were stopped.')
    archives=list(Path('dist/release').glob('*.zip'))
    if len(archives)!=1:raise RuntimeError('Expected exactly one native ZIP.')
    archive=archives[0].resolve()
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    assert archive.with_suffix('.zip.sha256').read_text().split()[0]==digest
    with tempfile.TemporaryDirectory(prefix='XZBot release 中文 ') as temporary:
        home=Path(temporary)
        with zipfile.ZipFile(archive) as package:
            bases={Path(n).parts[0] for n in package.namelist()}
            assert len(bases)==1
            for entry in package.infolist():
                target=(home/entry.filename).resolve()
                assert target.is_relative_to(home.resolve())
            package.extractall(home)
            if os.name!='nt':
                for entry in package.infolist():
                    if entry.external_attr>>16&0o111:(home/entry.filename).chmod(0o755)
        root=home/next(iter(bases));info=json.loads((root/'RELEASE-INFO.json').read_text())
        assert not (root/'data').exists() and not (root/'accounts').exists()
        entry={'Windows':'Start-XZBot.cmd','Darwin':'Start-XZBot.command','Linux':'Start-XZBot.sh'}[platform.system()]
        command=['cmd.exe','/d','/c',entry,'--no-open'] if os.name=='nt' else ['sh',entry,'--no-open']
        env={**os.environ,'CI':'true','PYTHONUTF8':'1','PYTHONPATH':str(root/'src'),'AZUSA_ROOT':str(root)}
        subprocess.run(command,cwd=root,env=env,check=True,timeout=600)
        profiles=json.loads((root/'data/profiles.json').read_text(encoding='utf-8'))
        assert [p['name'] for p in profiles['profiles']]==['阿梓','灰泽满']
        assert all(not p['voice']['voice_enabled'] and not p['voice']['voice_authorized'] for p in profiles['profiles'])
        python=root/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        subprocess.run([str(python),'scripts/smoke_release.py'],cwd=root,env=env,check=True,timeout=210)
    info.update(zip_sha256=digest,archive_install_test='passed',core_bridge_test='passed',
        unicode_directory_test='passed',profiles=['阿梓','灰泽满'],real_qq_login_test='not_performed',
        python=platform.python_version())
    archive.with_suffix('.verification.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Published archive launcher, fresh profiles, management and AstrBot bridge verified.')


if __name__=='__main__':main()
