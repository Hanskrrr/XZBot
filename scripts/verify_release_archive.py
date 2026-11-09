"""CI-only: install and smoke test the exact ZIP that will be published."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile


def main():
    if os.environ.get('CI')!='true':raise RuntimeError('Disposable CI verification only.')
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
        env={**os.environ,'PYTHONUTF8':'1','PYTHONPATH':str(root/'src'),'AZUSA_ROOT':str(root)}
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
