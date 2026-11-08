"""Idempotent native installer. All user state is preserved on subsequent starts."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PIN='61335414433db721b3f7ecc93f90deafe0a59d78'
ARCHIVE_SHA256='9d698bf65b80249cca48d68fcfd1f96b24e7d5b8c5d31d2909eb704f3308d097'


def py(venv):return venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')


def run(*args):
    subprocess.run(list(map(str,args)),cwd=ROOT,check=True,env={**os.environ,'PYTHONUTF8':'1'})


def environment(uv,venv,lock):
    if not py(venv).is_file():run(uv,'venv','--python','3.12',venv)
    stamp=venv/'.xzbot-dependencies'
    digest=hashlib.sha256(lock.read_bytes()+ (ROOT/'pyproject.toml').read_bytes()).hexdigest()
    if not stamp.exists() or stamp.read_text()!=digest:
        run(uv,'pip','install','--python',py(venv),'-r',lock)
        stamp.write_text(digest)


def astrbot_source():
    source=ROOT/'runtime/AstrBot'
    if source.exists():
        marker=source/'.xzbot-source-pin'
        if marker.exists() and marker.read_text().strip()==PIN:return source
        if (source/'.git').is_dir():
            commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
            if commit==PIN:return source
        raise RuntimeError('Existing AstrBot source differs; use a clean release directory.')
    source.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=source.parent,prefix='install-') as temporary:
        archive=Path(temporary)/'astrbot.zip'
        with urllib.request.urlopen('https://codeload.github.com/AstrBotDevs/AstrBot/zip/'+PIN,timeout=60) as response:
            with archive.open('wb') as out:shutil.copyfileobj(response,out)
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=ARCHIVE_SHA256:raise RuntimeError('AstrBot archive checksum mismatch.')
        with zipfile.ZipFile(archive) as z:
            for entry in z.infolist():
                destination=(Path(temporary)/entry.filename).resolve()
                if not destination.is_relative_to(Path(temporary).resolve()):raise RuntimeError('Invalid archive path.')
            z.extractall(temporary)
        extracted=Path(temporary)/('AstrBot-'+PIN)
        (extracted/'.xzbot-source-pin').write_text(PIN)
        extracted.rename(source)
    return source


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--no-open',action='store_true')
    parser.add_argument('--core-only',action='store_true')
    args=parser.parse_args()
    os.umask(0o077);os.environ['PYTHONUTF8']='1';os.environ['AZUSA_ROOT']=str(ROOT)
    uv=ROOT/'tools'/('uv.exe' if os.name=='nt' else 'uv')
    if not uv.is_file():uv=Path(shutil.which('uv') or '')
    if not uv.is_file():raise RuntimeError('uv not found. Download the complete platform release.')
    environment(uv,ROOT/'.venv',ROOT/'requirements-lock.txt')
    run(uv,'pip','install','--python',py(ROOT/'.venv'),'--no-deps','-e',ROOT)
    source=None
    if not args.core_only:
        source=astrbot_source()
        environment(uv,source/'.venv',ROOT/'requirements-astrbot.lock')
        run(uv,'pip','install','--python',py(source/'.venv'),'--no-deps','-e',source,'-e',ROOT)
    run(py(ROOT/'.venv'),ROOT/'scripts/release_initialize.py',*( ['--astrbot',str(source)] if source else []))
    if not args.no_open:
        run(py(ROOT/'.venv'),ROOT/'scripts/release_start.py')


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,subprocess.SubprocessError) as error:
        print('XZBot installation/start failed. Existing settings have been preserved.',file=sys.stderr)
        if isinstance(error,RuntimeError):print(str(error),file=sys.stderr)
        raise SystemExit(1) from None
