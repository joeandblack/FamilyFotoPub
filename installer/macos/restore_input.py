"""Password preflight and the nonsecret resume decision shared by Mac entry points."""
import json
from pathlib import Path

from app.backup import passwords
from app.migration import restore_stages
from app.backup.encrypted import InvalidArtifact, verify_artifact


def needs_backup(root):
    journal = Path(root) / 'restore/MOVE-IN.json'
    if not journal.exists():
        return True
    try:
        value = json.loads(journal.read_bytes())
        return restore_stages.needs_backup(value['stages'])
    except (OSError, ValueError, KeyError, TypeError):
        raise passwords.PasswordProblem(
            'The restore journal could not be read. Keep this installation folder and '
            'have restore/MOVE-IN.json inspected before continuing. Nothing was changed.') from None


def password_problem(backup, text):
    try:
        key = passwords.unlock_context(backup, passwords.BackupPassword(text))
        verify_artifact(Path(backup) / 'library', key)
    except passwords.PasswordProblem as error:
        return str(error)
    except InvalidArtifact:
        return 'The password opens this backup, but its library index is damaged. Choose another backup.'
    except OSError:
        return 'The backup could not be read. Reconnect its disk and select it again.'
    return ''


def close_input():
    """Detach the private input pipe before starting unrelated subprocesses."""
    import os
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(descriptor, 0)
    finally:
        os.close(descriptor)


def consume_password(args):
    """Read a secret before native children; never let them inherit the input pipe."""
    try:
        return passwords.from_arguments(args)
    finally:
        close_input()
