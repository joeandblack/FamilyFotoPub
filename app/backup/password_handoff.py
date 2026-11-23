"""Hand a password to one of FamilyFoto's own worker processes, and hear that it worked.

The parent writes one frame (app.backup.passwords.frame) to the child's private stdin pipe and
closes it. The child reads it, checks the password and derives its key, then answers on its
private stdout pipe with one line: `ready`, or `refused <message>`. Only after `ready` does
the parent go on, so a wrong password never stops FamilyFoto or starts work. The child then
points its stdout at its log (stderr), so nothing it prints later depends on the parent.

The password never appears in an argument, the environment, a file or a log. The pipes go to
this child only (close_fds), and the child gives its own children no stdin of its own.
"""
import json
import os
import subprocess
import sys
import threading

from . import passwords


READY_SECONDS = 120
FLAG = '--backup-password-frame-stdin'


class HandoffFailed(passwords.PasswordProblem):
    reason = 'password-handoff'


def launch(argv, text, *, cwd, log, timeout=READY_SECONDS, **options):
    """Start `argv` with the password on its private pipe; return the process once it is ready."""
    process = subprocess.Popen([*argv, FLAG], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=log, close_fds=True, **options)
    try:
        process.stdin.write(passwords.frame(text))
        process.stdin.close()
    except (OSError, passwords.PasswordProblem) as error:
        _stop(process)
        if isinstance(error, passwords.PasswordProblem):
            raise
        raise HandoffFailed('The worker process could not be given the password.') from None
    answer = []
    reader = threading.Thread(target=lambda: answer.append(process.stdout.readline(4096)), daemon=True)
    reader.start()
    reader.join(timeout)
    line = answer[0] if answer else b''
    if line.rstrip(b'\r\n') == b'ready':
        process.stdout.close()
        return process
    _stop(process)
    if line.startswith(b'refused '):
        try:
            value = json.loads(line[len(b'refused '):].decode('utf-8'))
        except (UnicodeError, ValueError):
            value = None
        if isinstance(value, dict) and isinstance(value.get('message'), str):
            error = HandoffFailed(value['message'][:500])
            error.reason = value.get('reason') if isinstance(value.get('reason'), str) else HandoffFailed.reason
            raise error
    raise HandoffFailed('The worker process did not start: it did not confirm it received the password.')


def _stop(process):
    try:
        process.kill()
    except OSError:
        pass
    for stream in (process.stdin, process.stdout):
        try:
            if stream:
                stream.close()
        except OSError:
            pass


def receive():
    """In the child: the one framed password, then stdin is closed for good."""
    try:
        return passwords.read_frame(sys.stdin.buffer)
    finally:
        try:
            sys.stdin.close()
            descriptor = os.open(os.devnull, os.O_RDONLY)
            os.dup2(descriptor, 0)
            os.close(descriptor)
        except OSError:
            pass


def _answer(line):
    stream = sys.stdout.buffer
    stream.write(line + b'\n')
    stream.flush()
    # From now on stdout goes where stderr goes: the parent stops listening after this line.
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def ready():
    _answer(b'ready')


def refuse(error):
    reason = getattr(error, 'reason', 'password')
    _answer(b'refused ' + json.dumps({'reason': reason, 'message': str(error)[:500]}).encode('utf-8'))
