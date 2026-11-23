"""Check a backup's administrator password before anything is installed.

The setup windows run this on a private, temporary Python that holds only the pinned
cryptography and cffi wheels (installer/windows/setup-password.ps1), before Start, so a wrong
password is found before FamilyFoto is installed. It uses the same password code as the
restore itself (app.backup.passwords): no platform has a second implementation.

    python -I -B installer/password_check.py --backup FOLDER [--site FOLDER]

One framed password (app.backup.passwords.frame) arrives on stdin; it is never an argument.
The answer is one JSON line on stdout, {"ok": true, "backup_id": ...} or
{"ok": false, "reason": ..., "message": ...}, and the exit code is 0 or 1. Nothing is written.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def answer(value):
    print(json.dumps(value), flush=True)
    return 0 if value.get('ok') else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--site', type=Path, help='The folder holding the pinned cryptography and cffi')
    args = parser.parse_args(argv)
    # -I leaves out the script's folder and the environment: name exactly what may be imported.
    sys.path[:0] = [str(path) for path in (args.site, ROOT) if path is not None]
    try:
        from app.backup import passwords
        from app.backup.encrypted import InvalidArtifact, verify_artifact
    except ImportError:
        return answer({'ok': False, 'reason': 'prerequisite',
                       'message': 'The password check could not start: its runtime is incomplete. Prepare it again.'})
    try:
        text = passwords.read_frame(sys.stdin.buffer)
        backup = Path(args.backup).absolute()
        if not (backup / 'library').is_dir():
            return answer({'ok': False, 'reason': 'not-a-backup',
                           'message': f'{backup} is not a finished FamilyFoto backup: it has no library index.'})
        key = passwords.unlock_context(backup, passwords.BackupPassword(text))
        del text
        try:
            verify_artifact(backup / 'library', key)
        except InvalidArtifact:
            return answer({'ok': False, 'reason': 'damaged-backup',
                           'message': 'The password opens this backup, but its library index is damaged. '
                                      'Use another backup, or make a new one.'})
        return answer({'ok': True, 'backup_id': key.backup_id})
    except passwords.PasswordProblem as error:
        return answer({'ok': False, 'reason': error.reason, 'message': str(error)})
    except OSError as error:
        return answer({'ok': False, 'reason': 'unreadable',
                       'message': f'The backup could not be read: {error.strerror or type(error).__name__}.'})


if __name__ == '__main__':
    sys.exit(main())
