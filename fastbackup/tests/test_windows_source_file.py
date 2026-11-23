import ctypes
from ctypes import wintypes as w
import os

import pytest

from fastbackup.windows_pinned_directory import PinnedDirectory, PinnedSourceFile

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Actual Windows file sharing')


def guid_path(path):
    path = path.resolve()
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetVolumePathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    api.GetVolumePathNameW.restype = w.BOOL
    api.GetVolumeNameForVolumeMountPointW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    api.GetVolumeNameForVolumeMountPointW.restype = w.BOOL
    mount, volume = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(1024)
    assert api.GetVolumePathNameW(str(path), mount, len(mount))
    assert api.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume))
    return volume.value + str(path)[len(mount.value):]


def test_file_readable_but_not_mutable_until_release(tmp_path):
    path = tmp_path / 'source.bin'
    path.write_bytes(b'original')
    with PinnedDirectory(guid_path(tmp_path)), PinnedSourceFile(guid_path(path)) as pinned:
        assert pinned.identity.size == 8
        assert path.read_bytes() == b'original'
        with pytest.raises(OSError):
            path.write_bytes(b'changed')
        with pytest.raises(OSError):
            path.unlink()
        with pytest.raises(OSError):
            path.rename(tmp_path / 'renamed')
        assert pinned.recheck() == pinned.identity
        assert path.read_bytes() == b'original'
    path.write_bytes(b'after release')
    path.rename(tmp_path / 'released')


def test_existing_writer_prevents_admission(tmp_path):
    path = tmp_path / 'busy.bin'
    path.write_bytes(b'original')
    with path.open('r+b'):
        with pytest.raises(OSError):
            PinnedSourceFile(guid_path(path))
    with PinnedSourceFile(guid_path(path)) as pinned:
        pinned.recheck()


def test_partial_admission_releases_acquired_handles(tmp_path):
    from contextlib import ExitStack
    path = tmp_path / 'first.bin'
    path.write_bytes(b'original')
    with pytest.raises(ValueError):
        with ExitStack() as stack:
            stack.enter_context(PinnedSourceFile(guid_path(path)))
            stack.enter_context(PinnedSourceFile(guid_path(tmp_path)))
    path.write_bytes(b'released')
    tmp_path.rename(tmp_path.with_name(tmp_path.name + '-released'))


def test_file_rejected_as_directory_without_leaking_handle(tmp_path):
    path = tmp_path / 'file.bin'
    path.write_bytes(b'data')
    with pytest.raises(ValueError):
        PinnedDirectory(guid_path(path))
    path.unlink()
