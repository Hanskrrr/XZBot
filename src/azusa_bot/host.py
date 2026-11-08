"""Host primitives: POSIX ownership/modes or Windows ACLs and byte-range locks.

Windows checks the opened handle's DACL rather than interpreting Unix mode bits.
No process-global monkey-patching of os or security checks.
"""
from pathlib import Path
import os
import socket
import stat
import time

WINDOWS = os.name == 'nt'
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def python_at(venv):
    return Path(venv) / ('Scripts/python.exe' if WINDOWS else 'bin/python')


def require_free_loopback_port(port):
    """Preflight a listener by binding, not by timing a refused connection.

    Windows can time out a loopback connect before reporting ECONNREFUSED.
    Exclusive bind also rejects a socket that is bound but not yet listening.
    The launcher's process lock remains responsible for duplicate launches.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if WINDOWS:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', port))


def _win_identity():
    import win32api, win32security
    handle = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try: return win32security.GetTokenInformation(handle, win32security.TokenUser)[0]
    finally: handle.Close()


def _win_acl():
    import win32security, ntsecuritycon
    acl = win32security.ACL()
    for sid in (_win_identity(), win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
            win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
            ntsecuritycon.FILE_ALL_ACCESS, sid)
    return acl


def protect(path):
    path=Path(path)
    if path.is_symlink(): raise ValueError('private_path_symlink')
    if WINDOWS:
        import win32security
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION,
            _win_identity(), None, _win_acl(), None)
    else: path.chmod(0o700 if path.is_dir() else 0o600)


def private_info(info, descriptor=None, path=None):
    if not WINDOWS:
        return info.st_uid == os.geteuid() and not info.st_mode & 0o077
    import win32security, msvcrt
    flags=win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION
    sd = (win32security.GetSecurityInfo(msvcrt.get_osfhandle(descriptor), win32security.SE_FILE_OBJECT, flags)
          if descriptor is not None else win32security.GetNamedSecurityInfo(str(path),win32security.SE_FILE_OBJECT,flags))
    user=_win_identity()
    if sd.GetSecurityDescriptorOwner() != user: return False
    acl=sd.GetSecurityDescriptorDacl()
    if acl is None:return False
    allowed={win32security.ConvertSidToStringSid(user),'S-1-5-18'}
    for index in range(acl.GetAceCount()):
        ace=acl.GetAce(index)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:return False
        if win32security.ConvertSidToStringSid(ace[2]) not in allowed:return False
    return bool(acl.GetAceCount())


def private_path(path):
    path=Path(path)
    return not path.is_symlink() and private_info(path.stat(),path=path)


def safe_open(path, flags, mode=0o600):
    """Do not follow a final symlink/reparse point, including on Windows."""
    if not WINDOWS:return os.open(path,flags | NOFOLLOW,mode)
    import win32file, win32con, pywintypes, win32security, msvcrt
    access=win32con.GENERIC_READ
    if flags & os.O_RDWR:access |= win32con.GENERIC_WRITE
    elif flags & os.O_WRONLY:access=win32con.GENERIC_WRITE
    creation=win32con.OPEN_EXISTING
    if flags & os.O_CREAT:creation=win32con.CREATE_NEW if flags & os.O_EXCL else win32con.OPEN_ALWAYS
    security=pywintypes.SECURITY_ATTRIBUTES()
    security.SECURITY_DESCRIPTOR.SetSecurityDescriptorDacl(1,_win_acl(),0)
    security.SECURITY_DESCRIPTOR.SetSecurityDescriptorControl(win32security.SE_DACL_PROTECTED,win32security.SE_DACL_PROTECTED)
    security.SECURITY_DESCRIPTOR.SetSecurityDescriptorOwner(_win_identity(),False)
    try:
        handle=win32file.CreateFile(str(path),access,win32con.FILE_SHARE_READ|win32con.FILE_SHARE_WRITE|win32con.FILE_SHARE_DELETE,
            security,creation,0x00200000,None)  # FILE_FLAG_OPEN_REPARSE_POINT
    except pywintypes.error as error:
        code=error.winerror
        if code in (2,3):raise FileNotFoundError(str(path)) from None
        if code in (80,183):raise FileExistsError(str(path)) from None
        if code==5:raise PermissionError(str(path)) from None
        raise OSError('private_file_open_failed') from None
    try:
        if win32file.GetFileInformationByHandle(handle)[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError('private_path_reparse_point')
        fd=msvcrt.open_osfhandle(handle.Detach(),flags | os.O_BINARY)
        if flags & os.O_TRUNC:os.ftruncate(fd,0)
        return fd
    finally:handle.Close()


if not WINDOWS:
    import fcntl as file_locks
else:
    class file_locks:
        LOCK_EX=2; LOCK_NB=4; LOCK_UN=8
        @staticmethod
        def flock(stream, flags):
            import msvcrt
            fd=stream if isinstance(stream,int) else stream.fileno()
            old=os.lseek(fd,0,os.SEEK_CUR)
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd,b'\0')
                while True:
                    os.lseek(fd,0,os.SEEK_SET)
                    try:
                        msvcrt.locking(fd,msvcrt.LK_UNLCK if flags & 8 else msvcrt.LK_NBLCK,1)
                        return
                    except OSError as error:
                        if flags & (4|8):raise BlockingIOError('file_lock_busy') from error
                        time.sleep(.05)
            finally:os.lseek(fd,old,os.SEEK_SET)


def link_directory(link, target):
    """Windows junctions need no Developer Mode; never copy login data."""
    link,target=Path(link),Path(target).resolve(strict=WINDOWS)
    if WINDOWS:
        import _winapi
        if link.exists():raise FileExistsError(str(link))
        # CPython 3.12 exposes the native junction primitive; no shell quoting
        # or Developer Mode is needed, including paths containing spaces.
        _winapi.CreateJunction(str(target),str(link))
    else:link.symlink_to(target,target_is_directory=True)
