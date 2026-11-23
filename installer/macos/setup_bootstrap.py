"""Temporary password-validation runtime; no Core files or services before Start."""
import os
import json
import shutil
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[2]


def environment():
    return {k: v for k, v in os.environ.items() if k not in ('PYTHONHOME', 'PYTHONPATH')}


def remove_unowned_runtime(folder, *, exclude=(), allow_unmarked=False):
    """Normal exit and crash recovery use the same conservative ownership check."""
    try:
        info = folder.lstat()
        marker = folder / 'owners.json'
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or marker.is_symlink()):
            return
        if not marker.exists() and allow_unmarked:
            shutil.rmtree(folder)  # Preparation failed before launching any child.
            return
        import fcntl
        with marker.open() as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            owners = json.load(stream)
            if not isinstance(owners, list) or not owners or any(type(pid) is not int or pid <= 0 for pid in owners):
                return
            for pid in set(owners) - set(exclude):
                try:
                    os.kill(pid, 0)
                    return
                except ProcessLookupError:
                    continue
            shutil.rmtree(folder)
    except (OSError, ValueError):
        pass  # Unknown, inaccessible or still-owned folders are left alone.


def clean_abandoned_runtimes():
    # Only our marked, private temporary directories; never installation or backup folders.
    for folder in Path(tempfile.gettempdir()).glob('familyfoto-setup-*'):
        remove_unowned_runtime(folder)


@contextmanager
def temporary_runtime():
    # TemporaryDirectory's finalizer would delete this even with a surviving restore child.
    folder = Path(tempfile.mkdtemp(prefix='familyfoto-setup-'))
    try:
        yield folder
    finally:
        remove_unowned_runtime(folder, exclude=(os.getpid(),), allow_unmarked=True)


def register_owner(marker=None, pid=None):
    marker = marker or os.environ.get('FAMILYFOTO_SETUP_OWNERS')
    if not marker:
        return
    import fcntl
    with Path(marker).open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        owners = json.loads(stream.read() or '[]')
        owners = sorted(set(owners + [pid or os.getpid()]))
        stream.seek(0)
        stream.truncate(0)
        json.dump(owners, stream)


def run_owned(command, folder, **kwargs):
    # Descendant coordinators register too: a closed window may leave a restore running.
    marker = folder / 'owners.json'
    register_owner(marker)
    kwargs['env'] = {**kwargs.get('env', environment()), 'FAMILYFOTO_SETUP_OWNERS': str(marker)}
    with subprocess.Popen(command, **kwargs) as child:
        register_owner(marker, child.pid)
        return child.wait()


def checked_owned(command, folder, **kwargs):
    if code := run_owned(command, folder, **kwargs):
        raise subprocess.CalledProcessError(code, command)


def prepare_runtime(folder):
    print('Preparing password validation (temporary private Python; no services or database changes)…', flush=True)
    env = environment()
    checked_owned([sys.executable, '-I', '-m', 'venv', str(folder)], folder.parent, stdin=subprocess.DEVNULL, env=env)
    python = folder / 'bin/python3'
    checked_owned([python, '-I', '-m', 'pip', 'install', '--no-cache-dir', *password_sources()],
                   folder.parent, stdin=subprocess.DEVNULL, env=env)
    return python


def password_sources():
    """The shared installer's verified local wheels when present; otherwise PyPI, hash-pinned."""
    if os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT'):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from installer.common.local_payloads import pip_arguments
        return pip_arguments('password')
    return ['--isolated', '--disable-pip-version-check', '--only-binary=:all:', '--require-hashes',
            '-r', str(ROOT / 'installer/macos/password-requirements.txt')]


def wants_window():
    """Terminal runs open the window as before; the FamilyFoto launcher app has no terminal."""
    return (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get('FAMILYFOTO_LAUNCHER') == '1'


def prebuilt_window():
    """The release's compiled setup window, verified by the Mac adapter, if this is a release install."""
    path = ROOT / 'bundled/setup-window/bin/familyfoto-setup'
    if os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT') and path.is_file() and not path.is_symlink() and os.access(path, os.X_OK):
        return path
    return None


def setup_window(folder):
    if (binary := prebuilt_window()) is not None:
        return binary
    print('Preparing the FamilyFoto setup window. The first launch may take a minute…', flush=True)
    binary = folder / 'setup'
    checked_owned(['/usr/bin/swiftc', '-parse-as-library', ROOT / 'installer/macos/SetupWindow.swift',
                    '-o', binary], folder, stdin=subprocess.DEVNULL, env=environment())
    return binary


def main(arguments):
    mode, *options = arguments
    clean_abandoned_runtimes()
    with temporary_runtime() as folder:
        python = prepare_runtime(folder / 'python')
        dialog = mode in ('install', 'restore') and not options and wants_window()
        if dialog and ((ROOT / '.macos-setup.json').is_file() or
                       (not (ROOT / 'runtime').exists() and not (ROOT / 'restore/MOVE-IN.json').exists())):
            binary = setup_window(folder)
            print('Opening FamilyFoto Setup…', flush=True)
            return run_owned([binary, ROOT, mode, python], folder, env=environment())
        if dialog and (ROOT / 'restore/MOVE-IN.json').is_file():
            mode = 'restore'
        return run_owned([python, '-B', ROOT / 'installer/macos/workflow.py', mode, *options],
                         folder, env=environment())


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, subprocess.CalledProcessError):
        sys.exit('Setup preparation failed. ' + ('Run setup again from the FamilyFoto launcher.'
                 if os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT') else
                 'Check the network and Homebrew Python, then run setup again.') + ' No restore was started.')
