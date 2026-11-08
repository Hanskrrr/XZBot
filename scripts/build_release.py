"""Package committed public files only; never walk the user's deployment tree."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import shutil
import sys
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def build(target,uv,out):
    if not uv.is_file():raise RuntimeError('Native uv executable was not found.')
    subprocess.run(['git','-C',str(ROOT),'diff','--exit-code','HEAD','--'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable,'scripts/check_repository.py','--ref','HEAD'],cwd=ROOT,check=True)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    source=subprocess.check_output(['git','archive','--format=zip','HEAD'],cwd=ROOT)
    import tomllib
    with zipfile.ZipFile(io.BytesIO(source)) as z:
        version=tomllib.loads(z.read('pyproject.toml').decode())['project']['version']
        suffix={'windows':'cmd','macos':'command','linux':'sh'}[target]
        arch={'AMD64':'x86_64','arm64':'aarch64'}.get(platform.machine(),platform.machine())
        base=f'XZBot-{version}-{target}-{arch}'
        out.mkdir(parents=True,exist_ok=True);destination=out/(base+'.zip')
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as package:
            for info in z.infolist():
                if info.is_dir() or info.filename.startswith(('.git/','.github/','.githooks/')):continue
                if info.filename.endswith('.command') and target!='macos':continue
                item=zipfile.ZipInfo(base+'/'+info.filename);item.external_attr=info.external_attr;item.compress_type=zipfile.ZIP_DEFLATED
                package.writestr(item,z.read(info))
            entry=zipfile.ZipInfo(base+'/Start-XZBot.'+suffix);entry.external_attr=0o100755<<16;entry.compress_type=zipfile.ZIP_DEFLATED
            # git archive honors eol attributes on the exporting host as well.
            # Normalize first so a CRLF input never becomes CRCRLF.
            launcher=z.read('scripts/release/Start-XZBot.'+suffix).replace(b'\r\n',b'\n').replace(b'\r',b'\n')
            package.writestr(entry,launcher.replace(b'\n',b'\r\n') if target=='windows' else launcher)
            binary=zipfile.ZipInfo(base+'/tools/'+('uv.exe' if target=='windows' else 'uv'));binary.external_attr=0o100755<<16;binary.compress_type=zipfile.ZIP_DEFLATED
            package.writestr(binary,uv.read_bytes())
            for license in ('LICENSE-MIT','LICENSE-APACHE'):
                package.writestr(base+'/tools/uv-'+license,(ROOT/'dist'/('uv-'+license)).read_bytes())
            package.writestr(base+'/RELEASE-INFO.json',json.dumps({'version':version,'platform':target,'arch':arch,'commit':commit,'private_data_included':False},indent=2))
            package.writestr(base+'/READ-ME-FIRST.md',z.read('docs/releases.md'))
    with zipfile.ZipFile(destination) as verify:
        launcher=verify.read(base+'/Start-XZBot.'+suffix)
        if target=='windows':
            assert b'\r\n' in launcher and b'\r\r\n' not in launcher
            assert verify.read(base+'/tools/uv.exe').startswith(b'MZ')
        else:
            assert b'\r' not in launcher
            assert verify.getinfo(base+'/Start-XZBot.'+suffix).external_attr >> 16 & 0o111
        assert verify.testzip() is None
    digest=hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.zip.sha256').write_text(digest+'  '+destination.name+'\n')
    print(destination.name)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--platform',required=True,choices=['windows','macos','linux']);p.add_argument('--uv',type=Path,default=Path(shutil.which('uv') or ''));p.add_argument('--out',type=Path,default=ROOT/'dist/release');args=p.parse_args();build(args.platform,args.uv,args.out)
