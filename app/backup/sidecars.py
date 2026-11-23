"""Recognize metadata files macOS adds on disks that have no extended attributes.

Only real AppleDouble `._*` and Finder `.DS_Store` files qualify, by their format as well
as their name. Callers apply this only to folders a job owns, or where an authenticated
inventory has the final word, so source media is never dropped because of its name.
"""
import os
import stat


APPLE_DOUBLE = b'\x00\x05\x16\x07'
DS_STORE = b'\x00\x00\x00\x01Bud1'


def is_sidecar(path, *, needs_sibling=True):
    """A tree entry needs the file it describes. A job's control folder can outlive it."""
    name = path.name
    if name == '.DS_Store':
        magic = DS_STORE
    elif name.startswith('._') and len(name) > 2:
        if needs_sibling and not os.path.lexists(path.with_name(name[2:])):
            return False
        magic = APPLE_DOUBLE
    else:
        return False
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
        with path.open('rb') as stream:
            return stream.read(len(magic)) == magic
    except OSError:
        return False
