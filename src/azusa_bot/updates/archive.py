"""Validate a release completely before making any changes to an installation."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

ROOT_FILES = {'README.md','LICENSE','pyproject.toml','requirements-lock.txt','requirements-astrbot.lock',
              'requirements-gemini-balance.lock','.gitignore','.gitattributes','RELEASE-INFO.json',
              'READ-ME-FIRST.md','RELEASE-FILES.json','Start-XZBot.cmd','Start-XZBot.command','Start-XZBot.sh',
              '启动QQ角色助手.command','启动管理台.command','停止QQ角色助手.command'}
DIRS = {'src','scripts','ui','docs','examples','tests','astrbot_plugin_azusa_bridge','astrbot_plugin_azusa_onebot','tools'}


def public_path(name):
    p = PurePosixPath(name)
    return (bool(name) and not p.is_absolute() and str(p) == name and '\\' not in name and ':' not in name
            and not any(x in {'.','..','data','content','accounts','runtime','.venv','.git','__pycache__'}
                        or x.endswith(('.', ' ')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', x)
                        for x in p.parts)
            and (name in ROOT_FILES or name == 'config/example.json' or len(p.parts)>1 and p.parts[0] in DIRS))


def validate(archive, release):
    if hashlib.sha256(Path(archive).read_bytes()).hexdigest() != release['sha256']:
        raise ValueError('update_checksum_mismatch')
    files = {}; total = 0
    with zipfile.ZipFile(archive) as z:
        bases = {x.filename.split('/')[0] for x in z.infolist()}
        if len(bases) != 1: raise ValueError('update_archive_invalid')
        base = next(iter(bases)); case_names = set()
        for entry in z.infolist():
            if entry.is_dir(): continue
            name = entry.filename.removeprefix(base+'/')
            total += entry.file_size
            if (not public_path(name) or name.casefold() in case_names
                    or stat.S_ISLNK(entry.external_attr>>16) or total>300*1024*1024
                    or len(files)>5000): raise ValueError('update_archive_invalid')
            case_names.add(name.casefold()); files[name]=(entry.filename, entry.external_attr>>16)
        info = json.loads(z.read(base+'/RELEASE-INFO.json'))
        if any(info.get(k)!=release[k] for k in ('version','platform','arch','commit')):
            raise ValueError('update_release_identity_mismatch')
        manifest = json.loads(z.read(base+'/RELEASE-FILES.json'))
        if set(manifest) != set(files)-{'RELEASE-FILES.json'}: raise ValueError('update_file_manifest_invalid')
        for name,digest in manifest.items():
            if hashlib.sha256(z.read(files[name][0])).hexdigest()!=digest:
                raise ValueError('update_file_manifest_invalid')
    return files


def safe_target(root, name):
    if not public_path(name): raise ValueError('update_private_path_refused')
    root=Path(root).resolve();path=root/name
    if any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,*path.parents) if p==root or root in p.parents):
        raise ValueError('update_link_refused')
    return path


def install_files(root, archive, release, backup):
    """Rollback journal covers only public program files, never content or credentials."""
    root=Path(root); backup=Path(backup)
    if (root/'.git').exists(): raise ValueError('update_source_checkout_refused')
    files=validate(archive,release)
    old_manifest=json.loads((root/'RELEASE-FILES.json').read_text())
    for name,digest in old_manifest.items():
        path=safe_target(root,name)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('update_local_code_modified')
    names=sorted(set(files)|set(old_manifest))
    # Validate every destination before the first replacement, including newly added files.
    for name in names:
        path=safe_target(root,name)
        if name not in old_manifest and name!='RELEASE-FILES.json' and path.exists():
            raise ValueError('update_local_code_modified')
    backup.mkdir(parents=True,exist_ok=False)
    journal={}
    for name in names:
        p=root/name;journal[name]=p.is_file()
        if p.exists() and not p.is_file():raise ValueError('update_path_not_file')
        if p.is_file():
            dest=backup/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    (backup/'journal.json').write_text(json.dumps(journal),encoding='utf-8')
    temporary_created=[]
    try:
        with zipfile.ZipFile(archive) as z:
            for name in names:
                path=root/name
                if name not in files:
                    if path.exists():path.unlink()
                    continue
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_name(path.name+'.xzbot-update')
                if temporary.exists() or temporary.is_symlink():raise ValueError('update_temporary_path_exists')
                member,mode=files[name]
                with temporary.open('xb') as out:
                    temporary_created.append(temporary);out.write(z.read(member))
                temporary.chmod(0o755 if mode&0o111 else 0o644)
                os.replace(temporary,path)
    except BaseException:
        restore(root,backup)
        raise
    finally:
        for temporary in temporary_created:temporary.unlink(missing_ok=True)


def restore(root,backup):
    root=Path(root);backup=Path(backup)
    for name,existed in json.loads((backup/'journal.json').read_text()).items():
        path=safe_target(root,name)
        if existed:shutil.copy2(backup/name,path)
        elif path.is_file():path.unlink()
