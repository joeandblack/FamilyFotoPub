"""Read-only entry identity handles; no copying or tree snapshot claim."""
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import os
import re


def require_guid_path(path):
    if not isinstance(path, str) or not re.fullmatch(
        r'\\\\\?\\Volume\{[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}\\[^\x00]*', path
    ):
        raise ValueError('An absolute volume-GUID path is required')
    suffix = path.split('}\\', 1)[1]
    if '/' in suffix or ':' in suffix or any(
        part in ('', '.', '..') or part.endswith((' ', '.'))
        for part in suffix.split('\\')
    ):
        raise ValueError('A canonical non-root directory path is required')
    return path


class _Info(ctypes.Structure):
    _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME),
                ('accessed', w.FILETIME), ('written', w.FILETIME),
                ('volume', w.DWORD), ('size_high', w.DWORD),
                ('size_low', w.DWORD), ('links', w.DWORD),
                ('index_high', w.DWORD), ('index_low', w.DWORD)]


@dataclass(frozen=True)
class DirectoryIdentity:
    final_path: str
    volume_serial: int
    file_index: int


@dataclass(frozen=True)
class FileIdentity(DirectoryIdentity):
    size: int
    last_write: int


class _PinnedEntry:
    _access = 0x80000000

    def __init__(self, path):
        self.path = require_guid_path(path)
        self._handle = None
        if os.name != 'nt':
            raise RuntimeError('Windows is required')
        self._api = ctypes.WinDLL('kernel32', use_last_error=True)
        api = self._api
        api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                   w.DWORD, w.DWORD, w.HANDLE]
        api.CreateFileW.restype = w.HANDLE
        api.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(_Info)]
        api.GetFileInformationByHandle.restype = w.BOOL
        api.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
        api.GetFinalPathNameByHandleW.restype = w.DWORD
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        # Attribute-only handles do not enforce the intended rename exclusion.
        handle = api.CreateFileW(self.path, self._access, self._share, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        try:
            self.identity = self._read_identity()
        except BaseException:
            self.close()
            raise

    def _read_identity(self):
        if self._handle is None:
            raise RuntimeError('Entry handle is closed')
        info = _Info()
        if not self._api.GetFileInformationByHandle(self._handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if bool(info.attributes & 0x10) != self._directory or info.attributes & 0x400:
            raise ValueError('Unexpected entry type or reparse point')
        output = ctypes.create_unicode_buffer(32768)
        length = self._api.GetFinalPathNameByHandleW(self._handle, output, len(output), 1)
        if not 0 < length < len(output):
            raise OSError('Cannot obtain complete GUID path from opened handle')
        if output.value.casefold() != self.path.casefold():
            raise ValueError('Opened entry does not match admitted GUID path')
        identity = (output.value, info.volume, (info.index_high << 32) | info.index_low)
        if self._directory:
            return DirectoryIdentity(*identity)
        return FileIdentity(*identity, (info.size_high << 32) | info.size_low,
                            (info.written.dwHighDateTime << 32) | info.written.dwLowDateTime)

    def recheck(self):
        if self._read_identity() != self.identity:
            raise ValueError('Pinned entry identity changed')
        return self.identity

    def close(self):
        if self._handle is not None:
            if not self._api.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PinnedDirectory(_PinnedEntry):
    """Pin one directory with READ/WRITE sharing, deliberately no DELETE share.

    Ancestors and descendants need separate admission/handles. New children can
    still be created: this does not make a mutable source tree safe to copy.
    """
    _share = 3
    _directory = True


class PinnedSourceFile(_PinnedEntry):
    """Allow readers but reject write/delete sharing on one existing source file.

    Fail if an incompatible writer is already open. No directory traversal,
    hashing or source snapshot is implied. Hold admitted ancestor handles too.
    """
    _share = 1
    _directory = False


class DeletableFile(PinnedSourceFile):
    """Delete only the admitted file handle, never a subsequently replaced pathname."""
    _access = 0x80000000 | 0x00010000

    def remove(self):
        self.recheck()
        api = self._api
        api.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        api.SetFileInformationByHandle.restype = w.BOOL
        delete = w.BOOL(True)
        if not api.SetFileInformationByHandle(self._handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())
