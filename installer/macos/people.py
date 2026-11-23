"""Guarded fresh Apple Silicon People setup. No application workers are started."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer.macos import manage, prepare
from installer.native_people import check_empty_people_schemas, verify


@contextmanager
def installation_lock(root):
    import fcntl
    # Same lock as the normal service, so bootstrap cannot overlap a running app.
    with (root / 'runtime/macos-service.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another installation or service is active') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def local_configuration(root, prefix):
    for target in (root, root / 'config', root / 'config/local.json', root / 'data', root / 'runtime', root / '.venv'):
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise ValueError('Linked installation paths are not allowed')
    if Path(prefix).resolve() != (root / '.venv').resolve():
        raise ValueError('Use this installation\'s own virtual environment')
    if os.environ.get('FAMILYFOTO_DATA_DIR'):
        raise ValueError('External data directory overrides are not allowed')
    cfg = json.loads((root / 'config/local.json').read_text())
    if any(cfg.get(k) for k in ('preview', 'live_existing_catalog', 'live_native_catalog', 'source', 'destination')):
        raise ValueError('Fresh setup requires an unconfigured, empty installation')
    if cfg.get('host') != '127.0.0.1' or cfg['database'].get('host') != '127.0.0.1':
        raise ValueError('Loopback-only configuration required')
    for path in (root / 'config/local.json', root / 'runtime', root / 'runtime/postgres'):
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError('Private configuration/runtime permissions required')
    return cfg


def check_fresh(root, cfg, port, adopt=False):
    if type(port) is not int or not 49152 <= port <= 65535:
        raise ValueError('People port must be between 49152 and 65535')
    if len({port, cfg['port'], cfg['database']['port']}) != 3:
        raise ValueError('Ports must be distinct')
    if cfg.get('face_detection'):
        raise ValueError('Existing provider configuration; inspect interrupted setup')
    # Adopting a restored library's catalog keeps its People data files; prepare points them here.
    restored = ('data/people-preview.json', 'data/face_detect', 'data/face-service-settings.json')
    for name in tuple(n for n in restored if not adopt) + ('runtime/face_detect', 'runtime/macos-people-install.json'):
        path = root / name
        if path.exists() or path.is_symlink():
            raise ValueError('Existing People state; no automatic adoption or overwrite')
    prepare.require_free_ports((port, cfg['port'], cfg['database']['port']))


def verify_database(cfg):
    manage.database_identity(cfg)
    spec = importlib.util.spec_from_file_location('mac_core_verify', ROOT / 'installer/verify-installation.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.database_checks(cfg, ROOT, require_empty=True)


@contextmanager
def database_window(cfg, binary):
    manage.validate_ready(cfg)
    prepare.require_free_ports((cfg['database']['port'], cfg['port']))
    with (ROOT / 'logs/macos-postgres.log').open('a') as log:
        process = subprocess.Popen(manage.postgres_args(cfg, binary), stdout=log, stderr=log,
                                   env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
        try:
            manage.wait_database(cfg, process)
            verify_database(cfg)
            yield
        finally:
            from app.database import close_pools
            close_pools()
            # Signal only the child we started, never a server found by port or pidfile.
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=60)


def install(research_only, port):
    if not research_only:
        raise ValueError('Explicit --research-only model consent required')
    cfg = local_configuration(ROOT, sys.prefix)
    with installation_lock(ROOT):
        cfg = local_configuration(ROOT, sys.prefix)
        check_fresh(ROOT, cfg, port)
        _, binary = manage.settings()
        with database_window(cfg, binary):
            check_empty_people_schemas(cfg)
            receipt = ROOT / 'runtime/macos-people-install.json'
            receipt.write_text(json.dumps({'state': 'installing', 'port': port}))
            from face_detect.cli.setup import setup
            from face_detect.cli.prepare import prepare as prepare_people
            from app.telemetry import write_json
            setup('insightface', research_only=True, activate=True)
            prepare_people(port=port)
            write_json(ROOT / 'data/face-service-settings.json', {'start_with_app': False})
            cfg = local_configuration(ROOT, sys.prefix)
            result = verify(ROOT, cfg)
        write_json(receipt, result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('install', 'verify'))
    parser.add_argument('--research-only', action='store_true')
    parser.add_argument('--port', type=int, default=54341)
    args = parser.parse_args()
    try:
        if sys.platform != 'darwin' or platform.machine() != 'arm64' or os.geteuid() == 0:
            raise ValueError('Run as the signed-in user on Apple Silicon')
        os.umask(0o077)
        if args.mode == 'install':
            result = install(args.research_only, args.port)
        else:
            # Read-only: requires the installation's database already running.
            cfg = local_configuration(ROOT, sys.prefix)
            verify_database(cfg)
            result = verify(ROOT, cfg)
        print(json.dumps(result))
    except Exception as error:
        print('People setup stopped (' + type(error).__name__ + '). Preserve partial state; no automatic retry or cleanup.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
