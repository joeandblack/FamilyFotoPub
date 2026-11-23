"""Exact-file, no-overwrite copying; callers own offline admission and verification."""
import ctypes
import os
from pathlib import Path
import stat
import sys

from app.backup.space import directory_identity


def flush_device(path):
    """Request Darwin drive-cache flush; unsupported devices fail closed."""
    if sys.platform == 'darwin':
        import fcntl
        with Path(path).open('rb') as stream:
            fcntl.fcntl(stream.fileno(), fcntl.F_FULLFSYNC)


def guid_path(path):
    """Resolve a local Windows path to its volume GUID, without accepting links."""
    path = Path(os.path.abspath(path))
    directory_identity(path if path.is_dir() else path.parent)
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    mount, volume = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(1024)
    for name in ('GetVolumePathNameW', 'GetVolumeNameForVolumeMountPointW'):
        function = getattr(api, name)
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_int
    if not api.GetVolumePathNameW(str(path.parent), mount, len(mount)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    relative = path.relative_to(Path(mount.value))
    return volume.value + str(relative)


def pin_directory(stack, path):
    directory_identity(Path(path))
    if sys.platform == 'win32':
        from .windows_pinned_directory import PinnedDirectory
        return stack.enter_context(PinnedDirectory(guid_path(path)))
    return None


def copy_file(source, target, *, cancelled=lambda: False):
    """New target only; source is operator-immutable and destination verified later."""
    source, target = Path(source), Path(target)
    directory_identity(source.parent)
    directory_identity(target.parent)
    before = source.lstat()
    if (not stat.S_ISREG(before.st_mode) or source.is_symlink() or source.is_junction()
            or getattr(before, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Only regular non-reparse source files may be copied')
    if os.path.lexists(target):
        raise FileExistsError('Fast copy never overwrites a destination')
    if cancelled():
        raise InterruptedError('Copy cancelled')
    if sys.platform == 'win32':
        from .windows_bulk_copy import copy_file as windows_copy
        windows_copy(source, target)
    else:
        # Exclusive descriptor-owned portable path; no shell tools or metadata copies.
        incoming = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(incoming)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError('Only regular source files may be copied')
            outgoing = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                               | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                while True:
                    if cancelled():
                        raise InterruptedError('Copy cancelled')
                    block = os.read(incoming, 8 * 1024 * 1024)
                    if not block:
                        break
                    remaining = memoryview(block)
                    while remaining:
                        written = os.write(outgoing, remaining)
                        if written <= 0:
                            raise OSError('Copy write made no progress')
                        remaining = remaining[written:]
                os.fsync(outgoing)
            finally:
                os.close(outgoing)
        finally:
            os.close(incoming)
