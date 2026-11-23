"""Fail-closed local macOS volume identity using diskutil plist metadata.

The compatibility serial fields hold partition and filesystem UUIDs, not a
claimed hardware serial. Device numbers are used only for live mount checks.
"""
import os
from pathlib import Path
import plistlib
import re
import subprocess
import uuid


def _error(message):
    from .disks import DiskError
    return DiskError(message)


def existing_parent(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise _error('Use an absolute local folder without parent traversal')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise _error('Linked storage folders are not allowed')
    while not path.exists():
        if path.parent == path:
            raise _error('Storage path is unavailable')
        path = path.parent
    return path


def info(target):
    result = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(target)],
                            capture_output=True, timeout=10)
    if result.returncode:
        raise _error('Volume unavailable. Reconnect the selected disk and check macOS Files and Folders permissions.')
    try:
        value = plistlib.loads(result.stdout)
        if value.get('Error'):
            raise ValueError('diskutil error')
        volume = str(uuid.UUID(value['VolumeUUID'])).upper()
        partition = str(uuid.UUID(value['DiskUUID'])).upper()
        mount = Path(value['MountPoint'])
        if not mount.is_absolute() or not mount.is_dir():
            raise ValueError('not mounted')
        if value.get('FilesystemType') not in ('apfs', 'hfs', 'exfat', 'msdos', 'ntfs'):
            raise ValueError('unsupported filesystem')
    except (ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        raise _error('A mounted local volume with partition and filesystem UUIDs is required') from None
    return {'drive':str(mount), 'serial':partition, 'volume_serial':volume,
            'volume_id':volume, 'online':True, 'writable':value.get('WritableVolume') is True,
            'filesystem':value['FilesystemType'], 'device':value['DeviceIdentifier']}


def drive_for_path(path):
    """df identifies the real volume, including the /Users APFS firmlink."""
    parent = existing_parent(path)
    result = subprocess.run(['/bin/df', '-P', str(parent)], capture_output=True, text=True, timeout=10)
    lines = result.stdout.splitlines()
    device = lines[-1].split()[0] if lines and lines[-1].split() else ''
    if result.returncode or not re.fullmatch(r'/dev/disk\d+(?:s\d+)*', device):
        raise _error('Choose a mounted local source volume; network mounts are not supported yet')
    observed = info(device)
    if parent.stat().st_dev != Path(observed['drive']).stat().st_dev:
        raise _error('Volume changed during path selection')
    return observed['drive']


def snapshot(drives):
    result = {}
    for drive in drives:
        path = Path(drive)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise _error('Selected disk is disconnected or no longer mounted; no writes permitted')
        observed = info(path)
        if observed['drive'] != str(path):
            raise _error('Mount point changed; no writes permitted')
        result[drive] = observed
    return result


def verify_path(expected, *, writable=False):
    from .disks import match_disk
    observed = snapshot([expected['drive']])[expected['drive']]
    match_disk(expected, observed)
    # Detect nested mounts or a stale ordinary /Volumes folder after disconnect.
    parent = existing_parent(expected['root'])
    if parent.stat().st_dev != Path(expected['drive']).stat().st_dev:
        raise _error('Storage folder is no longer on the enrolled volume')
    if writable and not observed['writable']:
        raise _error('Destination volume is read-only')
    return observed


def volume_attributes(path):
    """Live kernel volume UUID/mount flags; no subprocess or cached identity.

    ABI: Apple sys/attr.h ATTR_VOL_MOUNTPOINT, ATTR_VOL_MOUNTFLAGS,
    ATTR_VOL_UUID and ATTR_VOL_INFO.
    Attribute results are packed at four-byte boundaries in bitmap order.
    """
    import ctypes
    import struct
    class AttrList(ctypes.Structure):
        _fields_=[('count',ctypes.c_uint16),('reserved',ctypes.c_uint16),
                  ('common',ctypes.c_uint32),('volume',ctypes.c_uint32),
                  ('directory',ctypes.c_uint32),('file',ctypes.c_uint32),('fork',ctypes.c_uint32)]
    libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    call=libc.getattrlist
    call.argtypes=[ctypes.c_char_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong]
    call.restype=ctypes.c_int
    attrs=AttrList(5,0,0,0x80000000|0x00001000|0x00004000|0x00040000,0,0,0)
    buffer=ctypes.create_string_buffer(4096)
    if call(os.fsencode(path),ctypes.byref(attrs),buffer,len(buffer),0):
        raise _error('Cannot read live volume identity (errno '+str(ctypes.get_errno())+'); no writes permitted')
    length,offset,size,flags=struct.unpack_from('=IiII',buffer.raw)
    start=4+offset
    if not 32<=length<=len(buffer) or not 32<=start<length or not 1<=size<=length-start:
        raise _error('Unexpected native volume identity response')
    raw=buffer.raw[start:start+size]
    if raw[-1:]!=b'\0':raise _error('Invalid native mount path')
    value=uuid.UUID(bytes=buffer.raw[16:32])
    if not value.int:raise _error('Volume UUID unavailable; no writes permitted')
    return str(value).upper(),not bool(flags&1),os.fsdecode(raw[:-1]) # MNT_RDONLY


def fast_verify_path(expected, *, writable=False):
    """Per-chunk live UUID, mount, path-device and writable checks.

    Full partition UUID verification remains in Monitor.verify at preparation
    and every heartbeat. No positive disk identity is cached here.
    """
    mount=Path(expected['drive'])
    if mount.is_symlink() or not mount.is_dir():
        raise _error('Selected disk is disconnected or no longer mounted; no writes permitted')
    before=mount.stat().st_dev
    volume,can_write,mounted_at=volume_attributes(mount)
    if mounted_at!=str(mount):raise _error('Mount point changed; no writes permitted')
    for key in ('volume_serial','volume_id'):
        if volume.lower()!=str(expected[key]).strip().lower():
            raise _error('Disk identity mismatch ('+key+'); no writes permitted')
    parent=existing_parent(expected['root'])
    if parent.stat().st_dev!=before or mount.stat().st_dev!=before:
        raise _error('Storage folder is no longer on the enrolled volume')
    if writable and not can_write:raise _error('Destination volume is read-only')
