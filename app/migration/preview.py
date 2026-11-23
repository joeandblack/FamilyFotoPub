"""Explicit loopback-only recovery preview; never deploys or activates a library."""
import json
import os
from pathlib import Path
import subprocess
import sys

from ..backup.diagnostics import ExplainedFailure
from ..backup.encrypted import read_bounded
from ..backup.private_storage import create_private_directory
from .disk import disk_binding
from .preview_access import preview_database_access
from .validation import validate_plain_candidate, record


def preview_configuration(directory, tls_directory, scratch, database, port, *, probe=disk_binding):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Choose a non-privileged local preview port')
    cfg = json.loads(read_bounded(directory / 'catalog/files/config/local.json', 1024 ** 2))
    tls = record(tls_directory / 'TLS-REPORT.json')
    if '127.0.0.1' not in tls.get('ip_addresses', []):
        raise ValueError('Prepare the recovery TLS certificate with --name 127.0.0.1 for local preview')
    data = directory / 'catalog/files/data'
    people_path = data / 'people-preview.json'
    people_enabled = people_path.exists() and record(people_path).get('enabled') is True
    cfg.update(host='127.0.0.1', port=port, read_only_preview=True, database=database,
               source={}, destination=probe(Path(cfg['recovery']['media_root'])),
               tls={'certfile': str(tls_directory / 'server.pem'), 'keyfile': str(tls_directory / 'server-key.pem')},
               preview={'writable_root': str(scratch), 'data_root': str(data),
                        'people_readonly': people_enabled})
    return cfg


def launch_preview(cfg):
    """Fresh interpreter prevents preview monkeypatches from reaching a running app."""
    command = [sys.executable, '-B', '-m', 'app.migration.preview_server']
    environment = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2], env=environment,
                          stdin=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        try:
            process.stdin.write(json.dumps(cfg).encode())
            process.stdin.close()
            result = process.wait(timeout=3315)
            if result:
                raise RuntimeError('Recovery preview could not finish normally; its login has been revoked')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


def missing_packages(people):
    """What the preview server needs and this Python lacks. Checked before the media is read,
    since finding out afterwards costs the whole read again."""
    from importlib.util import find_spec
    needed = ['fastapi', 'uvicorn', 'psycopg', 'PIL'] + (['numpy', 'scipy'] if people else [])
    return [name for name in needed if find_spec(name) is None]


def preview_library(backup, key, directory, admin, role_directory, tls_directory, scratch, *,
                    expected_cluster_directory, port, launch=launch_preview, reverify=False,
                    packages=missing_packages):
    backup, directory, role_directory, tls_directory, scratch = (
        Path(path).absolute() for path in (backup, directory, role_directory, tls_directory, scratch))
    for protected in (backup, directory, role_directory, tls_directory, Path(expected_cluster_directory).resolve()):
        if scratch.is_relative_to(protected) or protected.is_relative_to(scratch):
            raise ValueError('Preview scratch must be independent of recovery inputs')
    if scratch.exists() or scratch.is_symlink():
        raise FileExistsError('Use a new preview scratch folder')
    # Quick refusals first: validation may read the whole library, which takes hours.
    cfg = preview_configuration(directory, tls_directory, scratch, {}, port)
    missing = packages(cfg['preview']['people_readonly'])
    if missing:
        raise ExplainedFailure('This Python is missing packages the preview needs: ' + ', '.join(missing)
                               + '. Install them, then run the preview again. Nothing was read or changed.')
    validate_plain_candidate(backup, key, directory, admin, role_directory, tls_directory,
                             expected_cluster_directory=expected_cluster_directory, reverify=reverify)
    # Check all presentation settings before enabling any database login.
    cfg = preview_configuration(directory, tls_directory, scratch, {}, port)
    create_private_directory(scratch)
    for name in ('logs', 'temp'):
        (scratch / name).mkdir()
    database_record = record(directory / 'catalog/database-target.json')
    with preview_database_access(admin, database_record, role_directory,
                                 expected_cluster_directory=expected_cluster_directory) as database:
        cfg['database'] = database
        launch(cfg)
    return {'state': 'preview-closed', 'activation_allowed': False, 'workers_started': False}
