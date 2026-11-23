"""Fresh native People provisioning; never adopts a live catalog or starts jobs."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@contextmanager
def installation_lock(root):
    import msvcrt
    path = root / 'data/setup'
    path.mkdir(parents=True, exist_ok=True)
    with (path / 'native-people.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError('Another native People installation is in progress') from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def local_configuration(root, prefix):
    for target in (root, root / 'config/local.json', root / 'data', root / 'runtime', root / '.venv'):
        if any(p.is_symlink() or p.is_junction() for p in (target, *target.parents)):
            raise ValueError('Linked installation paths are not allowed')
    if Path(prefix).resolve() != (root / '.venv').resolve():
        raise ValueError('Use this installation\'s own virtual environment')
    if os.environ.get('FAMILYFOTO_DATA_DIR'):
        raise ValueError('External data directory overrides are not allowed during fresh setup')
    cfg = json.loads((root / 'config/local.json').read_text(encoding='utf-8'))
    if cfg.get('preview') or cfg.get('live_existing_catalog') or cfg.get('live_native_catalog'):
        raise ValueError('Live or preview deployments cannot use fresh People setup')
    if cfg.get('source') or cfg.get('destination'):
        raise ValueError('Fresh setup requires storage not yet enrolled')
    if cfg.get('host') != '127.0.0.1' or cfg['database'].get('host') != '127.0.0.1':
        raise ValueError('Fresh setup requires loopback-only application and database')
    return cfg


def verify_database(root, cfg):
    import psycopg
    db = cfg['database']
    # Checking an empty database alone would not prove ownership of its cluster.
    with psycopg.connect(host=db['host'], port=db['port'], dbname='postgres',
                         user='postgres', password=cfg['postgres_admin_password'],
                         connect_timeout=5) as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        directory = connection.execute('SHOW data_directory').fetchone()[0]
        if Path(directory).resolve() != (root / 'data/postgres').resolve():
            raise ValueError('Database does not belong to this installation')
    spec = importlib.util.spec_from_file_location('installer_verify', root / 'installer/verify-installation.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.database_checks(cfg, root, require_empty=True)


def check_fresh(root, cfg, port, adopt=False):
    if cfg.get('face_detection'):
        raise ValueError('A provider is already configured; use Verify or inspect interrupted setup')
    # Adopting a restored library's catalog keeps its People files; prepare points them here.
    for name in (() if adopt else ('data/people-preview.json', 'data/face_detect', 'data/face-service-settings.json')):
        if (root / name).exists():
            raise ValueError('Existing People state found; no catalog will be replaced')
    for number in (port, cfg['port']):
        if number == cfg['database']['port']:
            raise ValueError('People/application ports must differ from the database port')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', number))
    if port == cfg['port']:
        raise ValueError('People and application ports must differ')


from installer.native_people import check_empty_people_schemas, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('install', 'verify'), required=True)
    parser.add_argument('--research-only', action='store_true')
    parser.add_argument('--port', type=int, default=54341)
    args = parser.parse_args()
    try:
        if sys.platform != 'win32' or not 49152 <= args.port <= 65535:
            raise ValueError('Windows and a People port between 49152 and 65535 are required')
        cfg = local_configuration(ROOT, sys.prefix)
        verify_database(ROOT, cfg)
        if args.mode == 'install':
            if not args.research_only:
                raise ValueError('Review model terms, then explicitly provide --research-only')
            with installation_lock(ROOT):
                cfg = local_configuration(ROOT, sys.prefix)
                # A library that already has the People catalog is adopted, with its files.
                check_fresh(ROOT, cfg, args.port, adopt=check_empty_people_schemas(cfg) == 'adopt')
                from face_detect.cli.setup import setup
                from face_detect.cli.prepare import prepare
                from app.telemetry import write_json
                setup('insightface', research_only=True, activate=True)
                prepare(port=args.port)
                write_json(ROOT / 'data/face-service-settings.json', {'start_with_app': False})
                cfg = json.loads((ROOT / 'config/local.json').read_text(encoding='utf-8'))
        result = verify(ROOT, cfg)
        print(json.dumps(result))
    except Exception as error:
        # Connection errors can contain credentials; do not print arbitrary exceptions.
        if type(error) is ValueError:
            print(str(error), file=sys.stderr)
        else:
            print('Native installation failed (' + type(error).__name__
                  + '); inspect local setup state before retrying.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
