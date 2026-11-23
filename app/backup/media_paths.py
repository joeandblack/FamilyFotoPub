"""Check inventoried paths fit a destination before any long copy starts.

Fails closed and never renames: library files keep the exact names the catalog and
inventory record. Error messages report counts and lengths, because file names can
be private. This checks path length only, not free space or filesystem features.
"""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sqlite3
import sys

from .media_inventory import InvalidMediaInventory


# NTFS, exFAT, HFS+ and APFS allow 255 UTF-16 units in a name. Others, like ext4, count bytes.
NAME_UNITS = 255
# Without long-path support: MAX_PATH less its NUL, and less 12 more for directories.
WINDOWS_FILE_UNITS = 259
WINDOWS_DIRECTORY_UNITS = 247
# The documented limit is approximate, so stay clearly below 32767.
WINDOWS_LONG_UNITS = 32000
MACOS_PATH_MAX = 1024
# Longest name the per-file copy job writes in its own directory.
LONGEST_JOB_FILE = 'copy.json.tmp'


class DestinationPathTooLong(InvalidMediaInventory):
    pass


@dataclass(frozen=True)
class PathLimits:
    file_units: int | None = None
    directory_units: int | None = None
    path_bytes: int | None = None
    name_bytes: int | None = None
    long_paths: bool | None = None


def windows_long_paths():
    """True only when both Windows policy and this process's manifest allow them."""
    import ctypes
    try:
        probe = ctypes.WinDLL('ntdll').RtlAreLongPathsEnabled
    except (OSError, AttributeError):
        return False
    probe.restype = ctypes.c_ubyte
    probe.argtypes = []
    return bool(probe())


def destination_limits(path):
    """Measure the limits that apply to this destination on this computer."""
    if os.name == 'nt':
        enabled = windows_long_paths()
        return PathLimits(file_units=WINDOWS_LONG_UNITS if enabled else WINDOWS_FILE_UNITS,
                          directory_units=WINDOWS_LONG_UNITS if enabled else WINDOWS_DIRECTORY_UNITS,
                          long_paths=enabled)
    existing = Path(os.path.abspath(path))
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    # PC_NAME_MAX is 255 on macOS volumes too, but there it does not count bytes.
    return PathLimits(path_bytes=posix_path_bytes(existing),
                      name_bytes=None if sys.platform == 'darwin' else os.pathconf(existing, 'PC_NAME_MAX'))


def posix_path_bytes(existing):
    """The kernel checks the path in encoded bytes; PATH_MAX includes the final NUL."""
    try:
        return os.pathconf(existing, 'PC_PATH_MAX') - 1
    except OSError:
        # macOS exFAT refuses this one question, but the limit is the kernel's own.
        if sys.platform != 'darwin':
            raise
        return MACOS_PATH_MAX - 1


def units(text):
    return len(text.encode('utf-16-le', 'surrogatepass')) // 2


def check_paths(inventory, tree, *, jobs=None, limits=None, progress=None):
    """Refuse when any entry cannot exist under `tree`, or its copy job under `jobs`."""
    inventory, tree = Path(inventory), Path(os.path.abspath(tree))
    limits = limits or destination_limits(tree)
    root = str(tree)
    long_files = long_directories = long_names = checked = 0
    longest = 0

    def measure(path, *, directory):
        nonlocal long_files, long_directories, longest
        size = units(path)
        limit = limits.directory_units if directory else limits.file_units
        over = limit is not None and size > limit
        if limits.path_bytes is not None and len(os.fsencode(path)) > limits.path_bytes:
            over = True
        if over:
            longest = max(longest, size)
            if directory:
                long_directories += 1
            else:
                long_files += 1

    with closing(sqlite3.connect(inventory.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        for name, kind in db.execute('SELECT path,kind FROM entries'):
            checked += 1
            if progress:
                progress({'phase': 'copy-paths', 'entries': checked})
            measure(root + os.sep + name.replace('/', os.sep), directory=kind == 'directory')
            for part in name.split('/'):
                if units(part) > NAME_UNITS or (limits.name_bytes is not None
                                                and len(os.fsencode(part)) > limits.name_bytes):
                    long_names += 1
                    break
            if jobs is not None and kind == 'file':
                job = os.path.join(os.path.abspath(jobs), hashlib.sha256(name.encode('utf-8')).hexdigest())
                measure(job, directory=True)
                measure(os.path.join(job, LONGEST_JOB_FILE), directory=False)
    if long_names:
        raise DestinationPathTooLong(
            f'{long_names} of {checked} media paths contain a name too long for this destination. '
            'Files are never renamed to fit; choose a destination whose filesystem allows these names.')
    if long_files or long_directories:
        advice = ('Use a shorter destination folder'
                  + (', or enable Windows long paths.' if limits.long_paths is False else '.'))
        raise DestinationPathTooLong(
            f'{long_files + long_directories} paths under this destination are too long '
            f'(longest {longest} characters). Nothing was copied. {advice}')
    return checked
