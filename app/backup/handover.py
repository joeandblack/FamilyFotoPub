"""Write the Mac restore instructions into a plain-media backup."""
from pathlib import Path
import logging
import os
import stat
import tempfile

from .space import directory_identity
from .media_copy import sync_directory
from .windows_handover import RECEIPT, read_release, write_windows_handover

FOLDER = 'restore-on-mac'
SOURCE = Path(__file__).resolve().parents[2] / 'installer/macos/handover'
FILES = ('README.txt',)
# Earlier backups carried a Terminal launcher that needed Homebrew; FamilyFoto Setup replaced it.
RETIRED = ('restore-familyfoto-mac.command',)
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
    for name in RETIRED:
        # A retried backup must not keep the old launcher beside the new instructions.
        _regular_or_absent(folder / name)
        (folder / name).unlink(missing_ok=True)
    write_release(folder, receipt_path)
    sync_directory(folder)
    write_windows_handover(backup, receipt_path)
