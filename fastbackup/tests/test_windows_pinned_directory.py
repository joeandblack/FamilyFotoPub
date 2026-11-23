import ctypes
from ctypes import wintypes as w
import os

import pytest

from fastbackup.windows_pinned_directory import PinnedDirectory, require_guid_path

GUID = '\\\\?\\Volume{827d6df9-b18d-4b9d-bbc8-d6dd988c1d4d}\\'


@pytest.mark.parametrize('path', [None, 'C:\\test', '\\\\server\\share', GUID,
    GUID + '../test', GUID + 'a\\..\\b', GUID + 'a\\\\b', GUID + 'a:stream',
    GUID + 'a.', GUID + 'a ', GUID + 'a\x00b'])
def test_reject_ambiguous_paths(path):
    with pytest.raises(ValueError):
        require_guid_path(path)


def test_valid_guid_path():
    assert require_guid_path(GUID + 'test\\nested') == GUID + 'test\\nested'


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows directory handles')
def test_pin_recheck_and_release(tmp_path):
    root = tmp_path.resolve()
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetVolumePathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    api.GetVolumePathNameW.restype = w.BOOL
    api.GetVolumeNameForVolumeMountPointW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    api.GetVolumeNameForVolumeMountPointW.restype = w.BOOL
    mount, volume = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(1024)
    assert api.GetVolumePathNameW(str(root), mount, len(mount))
    assert api.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume))
    path = volume.value + str(root)[len(mount.value):]
    with PinnedDirectory(path) as pinned:
        assert pinned.recheck() == pinned.identity
        with pytest.raises(OSError):
            root.rename(root.with_name(root.name + '-renamed'))
        child = root / 'child'
        child.mkdir()
        with PinnedDirectory(path + '\\child') as nested:
            nested.recheck()
            with pytest.raises(OSError):
                child.rename(root / 'replacement')
        child.rename(root / 'released')
    pinned.close()
    with pytest.raises(RuntimeError):
        pinned.recheck()


@pytest.mark.skipif(os.name == 'nt', reason='Non-Windows rejection')
def test_non_windows():
    with pytest.raises(RuntimeError, match='Windows'):
        PinnedDirectory(GUID + 'test')
