"""Write the Mac restore instructions, and for source installations the launcher, into a backup."""
from pathlib import Path
import logging
import os
import re
import stat
import subprocess
import tempfile

from .space import directory_identity
from .media_copy import sync_directory
from .windows_handover import RECEIPT, read_release, write_windows_handover

FOLDER = 'restore-on-mac'
SOURCE = Path(__file__).resolve().parents[2] / 'installer/macos/handover'
FILES = ('README.txt',)
# The Terminal launcher (Homebrew, Git) for installations made from source, which have no
# release to name. A release installation's backups use FamilyFoto Setup (RELEASE.txt) instead.
LAUNCHER = 'restore-familyfoto-mac.command'
# The launcher's empty commit line, filled in with the code that makes the backup.
EMPTY_COMMIT = b"commit=''\n"
ROOT = Path(__file__).resolve().parents[2]


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                            timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise OSError('git ' + args[0] + ' failed')
    return result.stdout.strip()


def source_commit(root=ROOT):
    """The commit of this installation's code, only when the code is shown to be exactly it; else ''.

    A Git checkout: HEAD, and no tracked file differs from it. A release folder (not a checkout):
    its release-manifest.json commit, and every file the manifest lists matches its SHA-256.
    Files outside those (untracked files in a checkout, or added to a release folder) are not
    covered. Anything else gets '', and the launcher then refuses rather than restore with other code."""
    try:
        if (root / '.git').exists():
            # Only in a checkout: on a Mac without Command Line Tools, git itself opens an install prompt.
            commit = _git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
            if _git(root, 'status', '--porcelain', '--untracked-files=no'):
                return ''  # local changes: HEAD is not the code that made this backup
        else:
            from installer.release import verify
            commit = verify(root)['source_commit']
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return ''
    return commit if isinstance(commit, str) and re.fullmatch('[0-9a-f]{40}', commit) else ''


def launcher(commit):
    """The Terminal launcher, set to download exactly `commit` (empty: it refuses to run)."""
    template = (SOURCE / LAUNCHER).read_bytes()
    if template.count(EMPTY_COMMIT) != 1:
        raise ValueError('Mac restore launcher has no commit line')
    return template.replace(EMPTY_COMMIT, f"commit='{commit}'\n".encode())
# Written only when this installation came from a shared-installer release.
RELEASE = 'RELEASE.txt'


def _regular_or_absent(target):
    # A retry can replace our old file or a partial publication, never follow a link.
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or target.is_symlink() or info.st_nlink != 1:
            raise ValueError('Linked or unexpected Mac handover file')


def _publish(folder, name, content, mode):
    target = folder / name
    _regular_or_absent(target)
    with tempfile.NamedTemporaryFile(dir=folder, prefix='.handover-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def release_text(release):
    return (
        'FamilyFoto installer release for restoring this backup on a Mac\n\n'
        f"Installer release: {release['release_id']}\n{release['setup_url']}\n\n"
        'Download FamilyFotoSetup.dmg from that link, on the official FamilyFoto GitHub releases\n'
        'page, and check its SHA-256 against the checksum published on that release page:\n'
        '  shasum -a 256 ~/Downloads/FamilyFotoSetup.dmg\n'
        'Do not open it if they differ. The app is not notarized by Apple: approve it once in\n'
        'System Settings, Privacy & Security, Open Anyway, only after the checksum matches.\n\n'
        'Open FamilyFoto Setup, choose Restore from a backup, select this backup folder\n'
        '(the parent of restore-on-mac), then in the next window enter the FamilyFoto administrator\n'
        'password used when this backup was made. Do not run programs merely because they are on this disk.\n'
        'For an offline restore, use the matching offline disk image obtained separately.\n\n'
        'Resume an interrupted restore from its installation folder:\n'
        '  bash installer/macos/install.sh restore\n'
        'If it says the downloaded installer files are gone, open FamilyFoto Setup again and\n'
        'choose the same installation folder.\n'
    ).encode('utf-8')


def write_release(folder, receipt_path=None):
    """Advisory: a bad or missing release record is logged and skipped, never fails a backup."""
    log = logging.getLogger(__name__)
    try:
        release = read_release(Path(receipt_path) if receipt_path else RECEIPT, 'mac_handover', 'macos-arm64')
    except ValueError:
        log.warning('Skipping Mac release instructions: invalid release receipt')
        release = None
    if release is not None:
        _publish(folder, RELEASE, release_text(release), 0o600)
        return True
    target = folder / RELEASE
    # Like the other handover files, a linked or unexpected file is refused, never left beside
    # the launcher where it could point a user at another installation's release.
    _regular_or_absent(target)
    target.unlink(missing_ok=True)  # A retry must not keep another installation's link.
    return False


def write_handover(backup, receipt_path=None):
    folder = Path(backup) / FOLDER
    folder.mkdir(exist_ok=True)
    directory_identity(folder)
    for name in FILES:
        _publish(folder, name, (SOURCE / name).read_bytes(), 0o600)
    if write_release(folder, receipt_path):
        # A retry must not keep the source launcher beside the release's instructions.
        _regular_or_absent(folder / LAUNCHER)
        (folder / LAUNCHER).unlink(missing_ok=True)
    else:
        commit = source_commit()
        if not commit:
            logging.getLogger(__name__).warning('The Mac restore launcher names no code version: this installation '
                                                'has local changes or cannot be identified, so it will refuse to run')
        _publish(folder, LAUNCHER, launcher(commit), 0o700)
    sync_directory(folder)
    write_windows_handover(backup, receipt_path)
