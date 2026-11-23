"""Repair ACLs on pinned NTFS objects inside the restored runtime directories."""
from contextlib import contextmanager, ExitStack
import ctypes as c
from ctypes import wintypes as w
import stat
import _winapi

from app.backup.diagnostics import ExplainedFailure

kernel = c.WinDLL('kernel32', use_last_error=True)
security = c.WinDLL('advapi32', use_last_error=True)
ptr = c.c_void_p
MAXIMUM_ALLOWED = 0x02000000
FILE_READ_ATTRIBUTES = 0x80
FILE_SHARE_READ = 1
OPEN_EXISTING = 3
OPEN_REPARSE_POINT = 0x00200000
BACKUP_SEMANTICS = 0x02000000
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 4
MODIFY = 0x1301bf


class FileInfo(c.Structure):
    _fields_ = [('attributes', w.DWORD), ('times', w.FILETIME * 3), ('volume', w.DWORD),
                ('size_high', w.DWORD), ('size_low', w.DWORD), ('links', w.DWORD),
                ('index_high', w.DWORD), ('index_low', w.DWORD)]


class Trustee(c.Structure):
    _fields_ = [('multiple', ptr), ('operation', w.DWORD), ('form', w.DWORD),
                ('type', w.DWORD), ('name', ptr)]


class Access(c.Structure):
    _fields_ = [('permissions', w.DWORD), ('mode', w.DWORD), ('inheritance', w.DWORD),
                ('trustee', Trustee)]


kernel.GetFileInformationByHandle.argtypes = [w.HANDLE, c.POINTER(FileInfo)]
kernel.LocalFree.argtypes = [ptr]
kernel.LocalFree.restype = ptr
security.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, c.POINTER(ptr)]
security.GetSecurityInfo.argtypes = [w.HANDLE, w.DWORD, w.DWORD, ptr, ptr, c.POINTER(ptr), ptr, c.POINTER(ptr)]
security.SetEntriesInAclW.argtypes = [w.DWORD, c.POINTER(Access), ptr, c.POINTER(ptr)]
security.SetSecurityInfo.argtypes = [w.HANDLE, w.DWORD, w.DWORD, ptr, ptr, ptr, ptr]


@contextmanager
def pinned(path, *, repair=False):
    # Deny write/delete sharing while traversing. OPEN_REPARSE_POINT opens a link
    # itself, so a replacement between enumeration and open is checked as well.
    try:
        handle = _winapi.CreateFile(str(path), MAXIMUM_ALLOWED if repair else FILE_READ_ATTRIBUTES,
                                    FILE_SHARE_READ, 0, OPEN_EXISTING, OPEN_REPARSE_POINT | BACKUP_SEMANTICS, 0)
    except OSError as error:
        if error.winerror != 32:  # ERROR_SHARING_VIOLATION
            raise
        raise ExplainedFailure(
            'A file needed for the access repair is open. Let active backups and workers finish, '
            'stop the FamilyFoto desktop server for this installation, then run Restore again. '
            'After a completed-library repair, restart the server from its desktop shortcut.'
        ) from error
    try:
        info = FileInfo()
        if not kernel.GetFileInformationByHandle(handle, c.byref(info)):
            raise c.WinError(c.get_last_error())
        directory = bool(info.attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
        if info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or (not directory and info.links != 1):
            raise ExplainedFailure(f'Linked entry in restored runtime data: {path}')
        yield handle, directory
    finally:
        _winapi.CloseHandle(handle)


def checked(status):
    if status:
        raise c.WinError(status)


def grant(handle, directory, sid):
    descriptor, old_acl, new_acl = ptr(), ptr(), ptr()
    try:
        checked(security.GetSecurityInfo(handle, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                         c.byref(old_acl), None, c.byref(descriptor)))
        if not old_acl:
            raise ExplainedFailure('Unexpected unrestricted ACL in restored runtime data.')
        # GRANT_ACCESS, TRUSTEE_IS_SID, TRUSTEE_IS_USER. Future children inherit
        # Modify. SetSecurityInfo on a MAXIMUM_ALLOWED handle does not propagate
        # to existing children; each is independently opened and validated.
        entry = Access(MODIFY, 1, 3 if directory else 0, Trustee(None, 0, 0, 1, sid))
        checked(security.SetEntriesInAclW(1, c.byref(entry), old_acl, c.byref(new_acl)))
        checked(security.SetSecurityInfo(handle, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                         None, None, new_acl, None))
    finally:
        kernel.LocalFree(new_acl)
        kernel.LocalFree(descriptor)


def visit(path, sid):
    with pinned(path, repair=True) as (handle, directory):
        grant(handle, directory, sid)
        if directory:
            for child in path.iterdir():
                visit(child, sid)


def repair_trees(data, owner):
    sid = ptr()
    if not security.ConvertStringSidToSidW(owner, c.byref(sid)):
        raise c.WinError(c.get_last_error())
    try:
        with ExitStack() as parents:
            for ancestor in (*reversed(data.parents), data):
                parents.enter_context(pinned(ancestor))
            for name in ('.recovery-admission', 'thumbnails', 'previews', 'people/thumbnails', 'face_detect'):
                path = data / name
                with ExitStack() as intermediate:
                    try:
                        if path.parent != data:
                            intermediate.enter_context(pinned(path.parent))
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    visit(path, sid)
    finally:
        kernel.LocalFree(sid)


def repair_media(home, owner, *, allow_unprotected=False):
    """Repair only the recorded restored media tree, never its catalog or backup."""
    from app.backup.volumes import windows_acls_persist

    sid = ptr()
    if not security.ConvertStringSidToSidW(owner, c.byref(sid)):
        raise c.WinError(c.get_last_error())
    try:
        with ExitStack() as parents:
            for ancestor in reversed(home.parents):
                parents.enter_context(pinned(ancestor))
            for path in (home, home / 'media-copy'):
                handle, directory = parents.enter_context(pinned(path, repair=True))
                if not directory:
                    raise ExplainedFailure('Restored media parent is not a directory.')
                if path == home and not windows_acls_persist(home):
                    if not allow_unprotected:
                        raise ExplainedFailure('Restored media disk cannot retain Windows permissions.')
                    return  # Explicitly accepted exFAT: do not claim an ACL was applied.
                grant(handle, directory, sid)
            visit(home / 'media-copy/media', sid)
    finally:
        kernel.LocalFree(sid)
