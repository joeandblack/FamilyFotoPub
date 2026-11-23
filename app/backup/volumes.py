"""Read-only volume identity probes for resumable backup jobs."""
import ctypes
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

from .space import directory_identity, StorageUnavailable


FILE_PERSISTENT_ACLS = 0x00000008


def same_except_device(recorded, current):
    """Compare a stored (device, inode, ...) record with this run's values.

    A device number can change when a disk remounts, so it means nothing across runs.
    Only use this beside an equal volume identity, which is what proves the same disk.
    """
    current = list(current)
    return isinstance(recorded, list) and len(recorded) == len(current) and recorded[1:] == current[1:]


def same_job(record, expected, identity):
    """A stored job record equals this run's, except the device in its `identity` field.

    `expected` must hold the job's volume identity, which proves the same disk.
    """
    return (isinstance(record, dict) and same_except_device(record.get(identity), expected[identity])
            and {**record, identity: expected[identity]} == expected)


def windows_acls_persist(path):
    """Whether this Windows volume really stores permissions. exFAT does not."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    mount = ctypes.create_unicode_buffer(32768)
    flags = wintypes.DWORD()
    get_mount = kernel.GetVolumePathNameW
    get_mount.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_mount.restype = wintypes.BOOL
    get_information = kernel.GetVolumeInformationW
    get_information.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPDWORD,
                                wintypes.LPDWORD, wintypes.LPWSTR, wintypes.DWORD)
    get_information.restype = wintypes.BOOL
    if (not get_mount(str(path), mount, len(mount))
            or not get_information(mount.value, None, 0, None, None, ctypes.byref(flags), None, 0)):
        raise StorageUnavailable('Cannot read Windows volume capabilities')
    return bool(flags.value & FILE_PERSISTENT_ACLS)


def windows_volume(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    mount = ctypes.create_unicode_buffer(32768)
    volume = ctypes.create_unicode_buffer(1024)
    get_mount = kernel.GetVolumePathNameW
    get_mount.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_mount.restype = wintypes.BOOL
    get_volume = kernel.GetVolumeNameForVolumeMountPointW
    get_volume.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_volume.restype = wintypes.BOOL
    if not get_mount(str(path), mount, len(mount)) or not get_volume(mount.value, volume, len(volume)):
        raise StorageUnavailable('Cannot identify local Windows backup volume')
    match = re.fullmatch(r'\\\\\?\\Volume\{([0-9a-fA-F-]{36})\}\\', volume.value)
    if not match:
        raise StorageUnavailable('Unsupported Windows volume identity')
    return {'kind': 'windows-volume-guid', 'id': match[1].lower()}


def macos_volume(path):
    try:
        result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(path)],
                                capture_output=True, timeout=15, check=True)
        data = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException) as error:
        raise StorageUnavailable('Cannot identify local macOS backup volume') from error
    identity = data.get('VolumeUUID') if isinstance(data, dict) else None
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', identity):
        raise StorageUnavailable('Backup volume lacks a persistent macOS UUID')
    return {'kind': 'macos-volume-uuid', 'id': identity.lower()}


def volume_identity(path):
    path = Path(os.path.abspath(path))
    before = directory_identity(path)
    if sys.platform == 'win32':
        result = windows_volume(path)
    elif sys.platform == 'darwin':
        mount = path
        while not os.path.ismount(mount):
            if mount == mount.parent:
                raise StorageUnavailable('Cannot locate macOS backup mount point')
            mount = mount.parent
        result = macos_volume(mount)
    else:
        # Linux is not yet a qualified migration platform. Keep development/test
        # copies usable within one boot, but refuse cross-boot resume explicitly.
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError as error:
            raise StorageUnavailable('Unsupported backup volume platform') from error
        if not re.fullmatch(r'[0-9a-f-]{36}', boot):
            raise StorageUnavailable('Invalid boot identity')
        result = {'kind': 'boot-scoped-device', 'id': f'{boot}:{before[0]}'}
    if directory_identity(path) != before:
        raise StorageUnavailable('Backup volume changed during identification')
    return result
