"""Explicit private-file credential backend for a migrated POSIX installation.

Never selected automatically after a Keychain error. The migration installer
creates a fixed marker; data stays outside source, mode 0600, directory 0700.
This is permission-protected storage, not encryption or a system keychain.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat

from .settings import private_write

MARKER = {'backend':'private-file-v1'}


def read_private(path: Path, maximum=600_000):
    if path.is_symlink():raise ValueError('private_credentials_path_invalid')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_size>maximum:
            raise ValueError('private_credentials_permissions_invalid')
        return json.load(stream)


class PrivateFileBackend:
    def __init__(self,root):
        self.directory=Path(root)/'data'
        if self.directory.is_symlink():raise ValueError('private_credentials_path_invalid')
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.directory.stat()
        if info.st_uid!=os.geteuid() or info.st_mode&0o077:raise ValueError('private_credentials_permissions_invalid')
        self.path=self.directory/'portable-credentials.json'

    @contextmanager
    def locked(self):
        import fcntl
        fd=os.open(self.directory/'portable-credentials.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
                raise ValueError('private_credentials_permissions_invalid')
            fcntl.flock(stream,fcntl.LOCK_EX)
            yield

    def read(self):
        try:data=read_private(self.path)
        except FileNotFoundError:return {'schema':1,'entries':{}}
        if not isinstance(data,dict) or set(data)!={'schema','entries'} or data['schema']!=1 or not isinstance(data['entries'],dict) or len(data['entries'])>64:
            raise ValueError('private_credentials_invalid')
        for key,value in data['entries'].items():
            if not re.fullmatch(r'[a-z][a-z0-9_]*(?::[a-f0-9]{64})?',key) or not isinstance(value,str) or len(value)>300_000:
                raise ValueError('private_credentials_invalid')
        return data

    def get_password(self,service,account):
        with self.locked():return self.read()['entries'].get(account)

    def set_password(self,service,account,value):
        with self.locked():
            data=self.read();data['entries'][account]=value
            private_write(self.path,json.dumps(data,ensure_ascii=False))

    def delete_password(self,service,account):
        with self.locked():
            data=self.read();data['entries'].pop(account,None)
            private_write(self.path,json.dumps(data,ensure_ascii=False))
