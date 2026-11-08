"""Explicit private migration: allowlisted source, SQLite snapshots and credentials.

No network requests and no automatic QQ startup. Call only for an authorized
private export. Secret values must never appear in return values or diagnostics.
"""
from contextlib import closing
import hashlib
import json
import os
import platform
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile

from . import __version__
from .accounts import AccountRegistry
from .credential_store import CredentialStore
from .portable_credentials import MARKER,PrivateFileBackend
from .secrets import resolve_credentials
from .settings import private_write,read_settings

SOURCE_DIRS=('src','scripts','ui','tests','docs','examples','astrbot_plugin_azusa_bridge','astrbot_plugin_azusa_onebot')
SOURCE_FILES=('README.md','LICENSE','pyproject.toml','requirements-lock.txt','requirements-astrbot.lock','requirements-gemini-balance.lock','.gitignore')
SKIP={'__pycache__','node_modules','.pytest_cache','.venv','.git','.DS_Store'}
DATABASES=('memory.sqlite3','group_state.sqlite3','state.sqlite3','bilibili.sqlite3','knowledge.sqlite3')
SECRET_NAMES=('deepseek_key','fish_key','compatible_key','gemini_keys','steam_api_key','bilibili_sessdata','bilibili_bili_jct','bilibili_buvid3','weibo_sub','weibo_cookie')
IDENTITY_FILES=('profiles.json','keychain-service','admin.token','bridge.token','onebot.token','napcat-webui.token','astrbot-admin.password','astrbot-onebot-admin.password')


def copy_file(source,dest):
    if source.is_symlink() or not source.is_file():raise ValueError('migration_source_invalid')
    dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    shutil.copyfile(source,dest);dest.chmod(0o600)


def copy_tree(source,dest):
    if source.is_symlink():raise ValueError('migration_symlink_rejected')
    if not source.exists():return
    dest.mkdir(parents=True,exist_ok=True,mode=0o700)
    for item in sorted(source.iterdir()):
        if item.name in SKIP or item.name.endswith(('.pyc','.log')):continue
        if item.is_symlink():raise ValueError('migration_symlink_rejected')
        if item.is_dir():copy_tree(item,dest/item.name)
        elif item.is_file():copy_file(item,dest/item.name)
        else:raise ValueError('migration_source_invalid')


def snapshot_db(source,destination):
    if not source.exists():return
    if source.is_symlink():raise ValueError('migration_database_symlink')
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('migration_database_invalid')
    destination.chmod(0o600)


def collect_credentials(root):
    settings=read_settings(root);store=CredentialStore(root)
    effective,sources=resolve_credentials(root,settings)
    entries=[]
    for name in SECRET_NAMES:
        base=settings.llm_base_url if name=='compatible_key' else ''
        value=getattr(effective,name) if name in {'deepseek_key','fish_key','compatible_key'} else store.get(name,base)
        if value:entries.append({'name':name,'base_url':base,'value':value})
    # Retain the unused official adapter's already-configured credential too,
    # without exporting unrelated DSH configuration or other applications' keys.
    legacy={key:getattr(effective,key) for key in ('qq_app_id','qq_secret') if getattr(effective,key)}
    return {'schema':1,'entries':entries,'legacy_official':legacy,
            'effective_voice':{'fish_reference_id':effective.fish_reference,'fish_model':effective.fish_model}}


def snapshot_account(root,dest):
    copy_tree(root/'config',dest/'config')
    copy_tree(root/'content',dest/'content')
    for name in DATABASES:snapshot_db(root/'data'/name,dest/'data'/name)
    for name in IDENTITY_FILES:
        if (root/'data'/name).exists():copy_file(root/'data'/name,dest/'data'/name)
    # Additional profiles contain only their own content and stores, no runtimes.
    profiles=json.loads((root/'data/profiles.json').read_text())
    for profile in profiles['profiles']:
        number=profile['id']
        if number=='default':continue
        if len(number)!=32 or any(c not in '0123456789abcdef' for c in number):raise ValueError('migration_profile_invalid')
        src=root/'data/profiles'/number;out=dest/'data/profiles'/number
        copy_tree(src/'content',out/'content')
        for name in DATABASES:snapshot_db(src/'data'/name,out/'data'/name)
    for kind in ('astrbot','astrbot-onebot'):
        base=root/'data'/kind/'data';out=dest/'data'/kind/'data'
        for name in ('cmd_config.json','mcp_server.json'):
            if (base/name).exists():copy_file(base/name,out/name)
        copy_tree(base/'config',out/'config')
        snapshot_db(base/'data_v4.db',out/'data_v4.db')
    # Gateway credentials and enabled choice are retained; no executable, pid,
    # cache, binary, virtual environment or machine-specific QQ session is copied.
    for name in ('control.json','data/access.token'):
        if (root/'runtime/gemini-gateway'/name).exists():copy_file(root/'runtime/gemini-gateway'/name,dest/'runtime/gemini-gateway'/name)
    if (root/'data/bot-control.json').exists():copy_file(root/'data/bot-control.json',dest/'original-bot-control.json')
    return len(profiles['profiles'])


def export_bundle(root,output,*,credential_reader=collect_credentials):
    root=Path(root).resolve();output=Path(output).absolute()
    if output.exists() or output.is_symlink():raise ValueError('migration_output_exists')
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    registry=AccountRegistry(root)
    records=[{'id':'primary','name':'主账号','slot':0},*registry.read()]
    started=time.time()
    with tempfile.TemporaryDirectory(prefix='.xzbot-private-',dir=output.parent) as scratch:
        stage=Path(scratch)/'XZBot-muse';stage.mkdir(mode=0o700);project=stage/'project'
        for name in SOURCE_DIRS:copy_tree(root/name,project/name)
        for name in SOURCE_FILES:
            if (root/name).exists():copy_file(root/name,project/name)
        copy_file(root/'config/example.json',project/'config/example.json')
        counts=[]
        for record in records:
            _,directory=registry.get(record['id']);out=stage/'private/accounts'/record['id']
            profiles=snapshot_account(directory,out/'snapshot')
            payload=credential_reader(directory)
            private_write(out/'credentials.json',json.dumps(payload,ensure_ascii=False))
            counts.append({'id':record['id'],'profiles':profiles,'credential_fields':len(payload['entries']),
                           'credential_names':[row['name'] for row in payload['entries']]})
        revision=subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True)
        info={'schema':1,'version':__version__,'created_at':time.time(),'snapshot_started_at':started,
              'contains_secrets':True,'contains_private_records':True,'accounts':records,
              'counts':counts,'source_head':revision.stdout.strip() if revision.returncode==0 else 'unknown',
              'source_is_working_tree':True,'qq_sessions_included':False}
        private_write(stage/'bundle.json',json.dumps(info,ensure_ascii=False,indent=2))
        guide=root/'docs/public-migration.md'
        if not guide.exists():guide=root/'docs/handoffs/muse-private-migration.md'
        # The entry is at the archive root; its original documentation links
        # were relative to project/docs rather than this location.
        guide_text=re.sub(r'\]\(([A-Za-z0-9_-]+\.md)\)', r'](project/docs/\1)', guide.read_text())
        private_write(stage/'START-HERE-MUSE.md',guide_text)
        private_write(stage/'integrity.json',json.dumps({p.relative_to(stage).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(stage.rglob('*')) if p.is_file()},indent=2))
        # Zip is private, not encrypted; its sensitive nature is explicit in its
        # name and manifest. No upload is performed by this operation.
        fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            with os.fdopen(fd,'wb') as stream,zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
                for p in sorted(stage.rglob('*')):
                    if p.is_file():archive.write(p,'XZBot-muse/'+p.relative_to(stage).as_posix())
        except BaseException:
            output.unlink(missing_ok=True);raise
    return {'version':__version__,'accounts':len(records),'profiles':sum(c['profiles'] for c in counts),
            'credential_fields':sum(c['credential_fields'] for c in counts),'bytes':output.stat().st_size,
            'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}


def verify_bundle(bundle):
    bundle=Path(bundle).resolve()
    manifest=json.loads((bundle/'integrity.json').read_text())
    if not isinstance(manifest,dict) or not manifest or len(manifest)>20000:raise ValueError('migration_manifest_invalid')
    actual={p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file() and p!=bundle/'integrity.json'}
    if actual!=set(manifest):raise ValueError('migration_file_set_changed')
    for name,digest in manifest.items():
        parts=PurePosixPath(name)
        if parts.is_absolute() or '..' in parts.parts or '\\' in name:raise ValueError('migration_path_invalid')
        p=bundle/name
        if any(node.is_symlink() for node in [p,*p.parents] if node.is_relative_to(bundle)):raise ValueError('migration_symlink_rejected')
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('migration_integrity_failed')
    info=json.loads((bundle/'bundle.json').read_text())
    if info.get('schema')!=1 or not info.get('contains_secrets'):raise ValueError('migration_manifest_invalid')
    return info


def restore_bundle(bundle,destination):
    """Restore into a new root, keeping every account paused. POSIX only."""
    if os.name!='posix':raise ValueError('migration_use_linux_or_wsl2')
    bundle=Path(bundle).resolve();dest=Path(destination).absolute()
    info=verify_bundle(bundle)
    if dest.exists() or dest.is_symlink():raise ValueError('migration_destination_exists')
    if not dest.parent.is_dir() or dest.parent.is_symlink():raise ValueError('migration_destination_parent_invalid')
    created=False
    try:
        dest.mkdir(mode=0o700);created=True
        copy_tree(bundle/'project',dest)
        rows=info['accounts']
        if not isinstance(rows,list) or not 1<=len(rows)<=4 or rows[0]['id']!='primary':raise ValueError('migration_accounts_invalid')
        for row in rows:
            number=row['id']
            if number!='primary':AccountRegistry.identifier(number)
            target=dest if number=='primary' else dest/'accounts'/number
            src=bundle/'private/accounts'/number
            copy_tree(src/'snapshot',target)
            payload=json.loads((src/'credentials.json').read_text())
            (target/'data').mkdir(mode=0o700,parents=True,exist_ok=True);(target/'data').chmod(0o700)
            private_write(target/'data/credential-backend.json',json.dumps(MARKER))
            backend=PrivateFileBackend(target)
            for entry in payload['entries']:
                account=CredentialStore.account(entry['name'],entry['base_url'])
                backend.set_password('',account,entry['value'])
            if payload.get('legacy_official'):
                private_write(target/'data/legacy-official-credentials.json',json.dumps(payload['legacy_official']))
            cfg=json.loads((target/'config/local.json').read_text());cfg['read_existing_dsh']=False
            for key,value in payload.get('effective_voice',{}).items():
                if value:cfg[key]=value
            if payload.get('effective_voice',{}).get('fish_model'):
                cfg['fish_model_override']=True
            private_write(target/'config/local.json',json.dumps(cfg,ensure_ascii=False,indent=2))
            # The Mac source may explicitly select Lima. A Linux receiver must
            # use its native NapCat connection, not inherit that host setting.
            private_write(target/'config/runtime.json',json.dumps({'mode':'lima' if platform.system()=='Darwin' else 'external'}))
            runtime_choice=target/'config/qq-runtime.json'
            if runtime_choice.exists():
                from .qq_runtime import read as read_runtime
                choice=read_runtime(target)
                # Runtime binaries and QQ login sessions are never in a migration bundle.
                choice['environment']='external'
                private_write(runtime_choice,json.dumps(choice))
            # Default-off is a persistent activity gate, retaining all user's
            # individual switches, subscription targets and whitelist values.
            private_write(target/'data/bot-control.json',json.dumps({'schema':1,'state':'stopped','epoch':uuid.uuid4().hex,'updated_at':time.time(),'error_code':''}))
            gateway=target/'runtime/gemini-gateway/control.json'
            if gateway.exists():copy_file(gateway,target/'original-gemini-control.json')
            private_write(gateway,json.dumps({'enabled':False}))
            if number!='primary':
                private_write(target/'config/instance.json',json.dumps({'schema':1,'slot':row['slot'],'manager_root':str(dest.resolve())}))
                for name in ('src','scripts','ui','astrbot_plugin_azusa_bridge','astrbot_plugin_azusa_onebot'):
                    (target/name).symlink_to(dest/name,target_is_directory=True)
                (target/'.venv').symlink_to(dest/'.venv',target_is_directory=True)
            private_write(target/'data/astrbot-source.path',str(dest/'runtime/AstrBot')+'\n')
            # Existing AstrBot configs are retained but automatic transport
            # setup is deferred to the receiving agent after QQ cutover.
        private_write(dest/'data/accounts.json',json.dumps({'schema':1,'accounts':rows[1:]},ensure_ascii=False))
        AccountRegistry(dest).read()  # Check rewritten paths and ownership.
        private_write(dest/'data/migration-receipt.json',json.dumps({'schema':1,'restored_at':time.time(),'source_version':info['version'],'all_accounts_paused':True}))
    except BaseException:
        if created:shutil.rmtree(dest)
        raise
    return {'accounts':len(rows),'all_accounts_paused':True,'credentials_imported':sum(c['credential_fields'] for c in info['counts'])}
