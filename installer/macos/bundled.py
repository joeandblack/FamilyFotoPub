"""Native programs bundled by the shared installer, used instead of Homebrew when present.

The Mac adapter (installer/macos/shared_adapter.py) unpacks them into `bundled/<role>` inside
the installation and sets FAMILYFOTO_PAYLOAD_RECEIPT. Without that receipt every caller keeps
its existing Homebrew behaviour. Standard library only: setup_bootstrap.py imports this before
any virtual environment exists.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FOLDER = 'bundled'
# Where the adapter records which prepared receipt this installation came from, for resume.
RECORD = 'receipt.json'
MARKER = '.shared-installer-release.json'
# Free space the setup window requires on the installation disk after the release is staged
# (installer/macos/setup_checks.py). The launcher's engine checks it before downloading anything.
WORKING_SPACE = 10 * 1024**3
EXECUTABLES = {'python': ('bin/python3.12',),
               'postgres': ('bin/initdb', 'bin/pg_ctl', 'bin/postgres'),
               'ffmpeg': ('bin/ffmpeg', 'bin/ffprobe'),
               # The setup window, compiled at release time: users need no Command Line Tools.
               'setup-window': ('bin/familyfoto-setup',)}


def active():
    return bool(os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT'))


def program_dir(role, root=None):
    """The `bin` folder of a bundled program, after checking its executables are present."""
    folder = Path(root if root is not None else ROOT) / FOLDER / role
    for relative in EXECUTABLES[role]:
        path = folder / relative
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f'Bundled {role} is missing {relative}; run setup again from the FamilyFoto launcher')
    return folder / 'bin'


def record_receipt(root, receipt, manifest_digest):
    """Remember the materialized receipt so `install.sh` can resume from a fresh shell."""
    folder = Path(root) / FOLDER
    # Normalized: the launcher deletes its session folder, so no `..` through it may remain.
    value = {'schema_version': 1, 'receipt': os.path.abspath(receipt), 'manifest_digest': manifest_digest}
    fd, temporary = tempfile.mkstemp(prefix='.receipt-', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder / RECORD)
    finally:
        Path(temporary).unlink(missing_ok=True)


def resume_receipt(root):
    """The recorded receipt path, after checking it still belongs to this installation."""
    root = Path(root)
    try:
        record = json.loads((root / FOLDER / RECORD).read_text(encoding='utf-8'))
        marker = json.loads((root / MARKER).read_text(encoding='utf-8'))
        receipt_path = Path(record['receipt'])
        matches = record.get('schema_version') == 1 and record['manifest_digest'] == marker.get('manifest_digest')
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError('The release record in this installation folder is unreadable') from error
    if not matches:
        raise ValueError('This installation folder does not match its release record')
    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('The downloaded installer files are gone. Open FamilyFoto Setup again and choose '
                         'this installation folder to carry on.') from None
    if receipt.get('stage') != 'materialized' or receipt.get('manifest_digest') != record['manifest_digest']:
        raise ValueError('The downloaded installer files no longer match this installation. Open FamilyFoto '
                         'Setup again and choose this installation folder to carry on.')
    return receipt_path


if __name__ == '__main__':
    # install.sh: `bundled/python/bin/python3.12 -I bundled.py resume-receipt ROOT` prints the path.
    if len(sys.argv) != 3 or sys.argv[1] != 'resume-receipt':
        sys.exit('Usage: bundled.py resume-receipt INSTALLATION_FOLDER')
    try:
        print(resume_receipt(sys.argv[2]))
    except ValueError as error:
        sys.exit(str(error))
