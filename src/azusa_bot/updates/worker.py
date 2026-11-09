"""Explicit update/restart worker. No QQ client, VM, or arbitrary PID is stopped."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from .archive import install_files, restore, validate
from .service import UpdateService, DEFAULTS


def owned_process(root, kind):
    import psutil
    filename={'core':'core.pid','bridge':'onebot-launch.pid'}[kind]
    path=root/'data'/filename
    if not path.exists():return None
    if path.is_symlink() or path.stat().st_size>30:raise ValueError('update_process_identity_invalid')
    pid=int(path.read_text().strip())
    if pid<=1 or pid==os.getpid():raise ValueError('update_process_identity_invalid')
    try:
        process=psutil.Process(pid);args=process.cmdline()
        if Path(process.cwd()).resolve()!=root.resolve():raise ValueError('update_process_identity_invalid')
        if kind=='core':
            if not any(args[i:i+3]==['-m','azusa_bot.cli','serve'] for i in range(len(args)-2)):
                raise ValueError('update_process_identity_invalid')
        elif os.path.normcase(str(root/'scripts/run_onebot_astrbot.py')) not in [os.path.normcase(arg) for arg in args]:
            raise ValueError('update_process_identity_invalid')
        return process
    except psutil.NoSuchProcess:return None


def wait_stopped(process, timeout=40):
    """A detached POSIX process can remain a zombie until its parent reaps it.

    Zombies have already released sockets and locks; psutil.wait() on a
    non-child can otherwise wait forever for an already-finished process.
    is_running() also checks PID reuse before we inspect the status.
    """
    import psutil
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            if not process.is_running() or process.status() in {psutil.STATUS_ZOMBIE,psutil.STATUS_DEAD}:return
        except psutil.NoSuchProcess:return
        time.sleep(.1)
    raise ValueError('update_process_stop_timeout')


def backup_data(roots, destination):
    """SQLite backup API covers WAL content; no credential-store export."""
    for index,root in enumerate(roots):
        for path in (root/'data').rglob('*.sqlite3'):
            relative=path.relative_to(root/'data')
            if 'backups' in relative.parts or 'updates' in relative.parts or path.is_symlink():continue
            target=destination/str(index)/relative;target.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(path) as source,sqlite3.connect(target) as output:source.backup(output)


def main(root):
    root=Path(root).resolve();sys.path.insert(0,str(root/'scripts'))
    from start_onebot import startup_guard, ensure_core, ensure_bridge
    from ..accounts import AccountRegistry
    from ..host import file_locks
    service=UpdateService(root)
    if not service.supported():raise ValueError('update_release_installation_required')
    service.directory.mkdir(parents=True,exist_ok=True)
    with (service.directory/'install.lock').open('a') as lock:
        file_locks.flock(lock,file_locks.LOCK_EX|file_locks.LOCK_NB)
        pending=service.read('pending.json',None)
        if not pending or pending['feed_url']!=service.configuration()['feed_url']:
            raise ValueError('update_source_changed')
        validate(service.directory/'pending.zip',pending['release'])
        # Let the HTTP acknowledgement leave before stopping its own server.
        time.sleep(1.5)
        registry=AccountRegistry(root);roots=[root]+[registry.child_root(r['id']) for r in registry.read()]
        backup=service.directory/('rollback-'+time.strftime('%Y%m%d-%H%M%S'))
        processes=[];stopped=[];applied=False
        service.write('operation.json',{'state':'installing','at':time.time(),'version':pending['release']['version']})
        with ExitStack() as guards:
            for directory in roots:guards.enter_context(startup_guard(directory))
            # Resolve all identities before changing the first process.
            for directory in roots:
                for kind in ('bridge','core'):
                    process=owned_process(directory,kind)
                    if process is not None:processes.append((directory,kind,process))
            try:
                for directory,kind,process in processes:
                    process.terminate();stopped.append((directory,kind))
                for _,_,process in processes:wait_stopped(process)
                # A supervisor cannot restart services while startup guards are held.
                backup_data(roots,backup/'databases')
                install_files(root,service.directory/'pending.zip',pending['release'],backup/'program')
                applied=True
                env={**os.environ,'AZUSA_ROOT':str(root),'PYTHONPATH':str(root/'src'),'PYTHONUTF8':'1'}
                result=subprocess.run([sys.executable,str(root/'scripts/install_release.py'),'--no-open'],
                                      cwd=root,env=env,timeout=600,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if result.returncode:raise ValueError('update_dependency_install_failed')
                # A fresh interpreter verifies the new code instead of using old imported modules.
                for directory,kind in reversed(stopped):
                    env={**env,'AZUSA_ROOT':str(directory),'PYTHONPATH':str(directory/'src')}
                    result=subprocess.run([sys.executable,str(root/'scripts/update_start_services.py'),kind],
                                          cwd=directory,env=env,timeout=60,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    if result.returncode:raise ValueError('update_restart_failed')
                service.write('pending.json',None)
                service.write('operation.json',{'state':'installed','at':time.time(),'version':pending['release']['version'],
                                               'backup':backup.name})
            except Exception as error:
                # Do not overwrite code while a newly started service is still using it.
                if applied:
                    for directory,kind in stopped:
                        process=owned_process(directory,kind)
                        if process is not None:process.terminate();wait_stopped(process)
                    restore(root,backup/'program')
                    subprocess.run([sys.executable,str(root/'scripts/install_release.py'),'--no-open'],cwd=root,
                        env={**os.environ,'AZUSA_ROOT':str(root),'PYTHONPATH':str(root/'src')},
                        timeout=600,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                for directory,kind in reversed(stopped):
                    if kind=='core':ensure_core(directory)
                    else:ensure_bridge(directory)
                code=str(error) if isinstance(error,ValueError) and str(error).startswith('update_') else 'update_install_failed'
                service.write('operation.json',{'state':'failed','error':code,'at':time.time(),'backup':backup.name})
                raise ValueError(code) from None


if __name__=='__main__':
    try:main(sys.argv[1])
    except Exception as error:
        try:
            service=UpdateService(Path(sys.argv[1]).resolve())
            if service.supported():
                code=str(error) if isinstance(error,ValueError) and str(error).startswith('update_') else 'update_install_failed'
                previous=service.read('operation.json',{})
                service.write('operation.json',previous|{'state':'failed','error':code,'at':time.time()})
        except Exception:pass
        print('XZBot update did not complete. See management update status and the local rollback directory.')
        raise SystemExit(1) from None
