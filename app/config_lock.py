"""One lock for every writer of config/local.json, across threads and processes.

Hold it around read, change, revision check and replacement, so two saves can never both
pass their check and then overwrite each other. The lock file holds no data; backups treat
it as control state and never capture it.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time

LOCK_NAME = 'local.json.lock'
_threads = threading.Lock()


def _try_lock(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def config_write_lock(config_file, timeout=15):
    path = Path(config_file).parent / LOCK_NAME
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Linked settings lock')
    deadline = time.monotonic() + timeout
    if not _threads.acquire(timeout=timeout):
        raise RuntimeError('Settings are being saved; try again')
    try:
        with path.open('a+b') as handle:
            while True:
                try:
                    _try_lock(handle)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Settings are being saved by another process; try again') from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                _unlock(handle)
    finally:
        _threads.release()
