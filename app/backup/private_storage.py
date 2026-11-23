"""Create new private restore directories before writing any decrypted state."""
import csv
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from .space import directory_identity
from .volumes import windows_acls_persist


class UnprotectedDestination(PermissionError):
    pass


def refuse_unprotected(path):
    # Created just above, and still empty: nothing private has been written.
    path.rmdir()
    raise UnprotectedDestination(
        'This disk cannot keep files private, which is true of exFAT. Nothing was written. Use NTFS or APFS, '
        'or explicitly accept that anyone with the disk can read what is written there.')


def keeps_permissions(path):
    """Leave `path` owner-only, and say whether the disk really stores its mode.

    macOS exFAT reports 0o700 whatever is set, so one matching mode proves nothing.
    Only a disk that reports two different modes back is keeping them.
    """
    try:
        for mode in (0o500, 0o700):
            path.chmod(mode)
            if stat.S_IMODE(path.stat().st_mode) != mode:
                path.chmod(0o700)
                return False
    except OSError:
        return False
    return True


def create_private_directory(path, *, allow_unprotected=False):
    """Create a new directory, refusing a disk that would ignore its permissions.

    Only pass `allow_unprotected` for output the operator explicitly accepted as readable
    by anyone with the disk. Never for keys, credentials or restored application state.
    """
    path = Path(os.path.abspath(path))
    directory_identity(path.parent)
    path.mkdir(mode=0o700)  # Refuse existing targets; never change their permissions.
    if os.name != "nt":
        if not keeps_permissions(path) and not allow_unprotected:
            refuse_unprotected(path)
        return path
    # icacls reports success on exFAT while storing nothing, so its result proves nothing there.
    if not windows_acls_persist(path):
        if not allow_unprotected:
            refuse_unprotected(path)
        return path
    system = Path(os.environ["SystemRoot"]) / "System32"
    options = {"capture_output": True, "creationflags": subprocess.CREATE_NO_WINDOW, "timeout": 15}
    result = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"], **options)
    rows = list(csv.reader(io.StringIO(result.stdout.decode(errors="replace"))))
    if result.returncode or len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"S-1-(?:\d+-)*\d+", rows[0][1]):
        raise PermissionError("Cannot identify restore directory owner; no secrets were written")
    sid = rows[0][1]
    result = subprocess.run([str(system / "icacls.exe"), str(path), "/inheritance:r", "/grant:r",
                             f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], **options)
    if result.returncode:
        raise PermissionError("Cannot protect restore directory; no secrets were written")
    return path


def check_destination_privacy(parent, *, allow_unprotected=False):
    """Refuse an unprotected disk early, before a gate is taken or any long work starts."""
    create_private_directory(Path(parent) / f'.familyfoto-privacy-check-{uuid.uuid4().hex}',
                             allow_unprotected=allow_unprotected).rmdir()
