"""Release-pinned Windows restore instructions; never execute backup-side code."""
import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path
from installer.common.manifest import validate_release_link

from .space import directory_identity
from .media_copy import sync_directory

FOLDER = 'restore-on-windows'
RECEIPT = Path(__file__).resolve().parents[2] / '.shared-installer-release.json'
LAUNCHER = 'restore-familyfoto-windows.ps1'
SOURCE = Path(__file__).resolve().parents[2] / 'installer/windows/handover'


def write_source_handover(backup, commit):
    """Source fallback; an unidentified installation gets a launcher that refuses early."""
    from .handover import _publish
    if commit and not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid Windows source commit')
    template = (SOURCE / LAUNCHER).read_bytes().replace(b'\r\n', b'\n')
    marker = b"$commit = ''\n"
    if template.count(marker) != 1:
        raise ValueError('Windows restore launcher has no commit line')
    folder = Path(backup) / FOLDER
    folder.mkdir(exist_ok=True)
    directory_identity(folder)
    if not commit:
        logging.getLogger(__name__).warning('Windows restore launcher has no verified code version and will refuse to run')
    text = ('Restore a source installation on Windows\n\n'
            f'Exact source commit: {commit or "unidentified - launcher disabled"}\n'
            'Obtain trusted restore instructions from the official FamilyFoto repository.\n'
            'Do not run programs merely because they are on this backup disk.\n'
            'The included restore-familyfoto-windows.ps1 requires Git and an administrator\n'
            'PowerShell window. Choose a fresh local NTFS installation folder; then select\n'
            'this backup in Restore setup and enter its administrator password.\n'
            'It downloads only the recorded commit, never a moving branch.\n')
    _publish(folder, 'README.txt', text.encode(), 0o600)
    _publish(folder, LAUNCHER, template.replace(marker, f"$commit = '{commit}'\n".encode()), 0o600)
    sync_directory(folder)


def read_release(receipt_path, key='windows_handover', platform='windows-x64'):
    """The release link an installation's ownership marker records for `platform`, or None."""
    try:
        if not receipt_path.exists():
            return None
        if receipt_path.is_symlink() or receipt_path.stat().st_nlink != 1:
            raise ValueError('Linked release receipt')
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        release = receipt.get(key)
        if release is not None:
            validate_release_link(release['release_id'], release['setup_url'], platform)
        return release
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid installer release receipt') from exc


def discard_stale_readme(backup):
    folder = Path(backup) / FOLDER
    if not folder.exists():
        return
    directory_identity(folder)
    target = folder / 'README.txt'
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or target.is_symlink() or info.st_nlink != 1:
            raise ValueError('Unsafe stale handover file; manual review required')
        target.unlink()
        sync_directory(folder)


def write_windows_handover(backup, receipt_path=None):
    receipt_path = Path(receipt_path) if receipt_path is not None else RECEIPT
    try:
        release = read_release(receipt_path)
    except ValueError:
        logging.getLogger(__name__).warning('Skipping Windows restore instructions: invalid release receipt')
        release = None
    if release is None:
        try:
            discard_stale_readme(backup)
        except (OSError, ValueError):
            logging.getLogger(__name__).warning('Stale Windows restore instructions require manual removal')
        return False
    url = release['setup_url']
    text = (
        'Restore FamilyFoto on Windows\n\n'
        f"Installer release: {release['release_id']}\n{url}\n\n"
        'Download this release from the official FamilyFoto GitHub repository.\n'
        'Do not run executable files merely because they are on this backup disk.\n'
        '\n'
        'Open FamilyFotoSetup.exe and choose Restore in the setup window. Select\n'
        'this backup folder (the parent of restore-on-windows), then enter the\n'
        'FamilyFoto administrator password used when this backup was made.\n'
        'To preselect it, run the downloaded launcher with --backup followed by\n'
        'the quoted full path to this backup folder. Do not start a new library.\n\n'
        'Resume an interrupted restore from its installation folder:\n'
        'powershell.exe -ExecutionPolicy Bypass -File installer\\windows\\install.ps1 -Mode Restore\n'
        'Keep the backup until the restored library has been checked.\n'
    ).encode('utf-8')
    folder = Path(backup) / FOLDER
    folder.mkdir(exist_ok=True)
    directory_identity(folder)
    from .handover import _regular_or_absent
    _regular_or_absent(folder / LAUNCHER)
    (folder / LAUNCHER).unlink(missing_ok=True)
    target = folder / 'README.txt'
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or target.is_symlink() or info.st_nlink != 1:
            raise ValueError('Linked Windows handover instructions refused')
    with tempfile.NamedTemporaryFile(dir=folder, prefix='.handover-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    sync_directory(folder)
    return True
