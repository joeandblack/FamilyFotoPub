import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from app.backup.postgres import Endpoint
from app.migration.preview import preview_configuration
from app.migration.preview_access import preview_database_access
from app.migration.preview_server import check_configuration
from app.migration.tls import prepare_recovery_tls
from tests.test_backup_postgres import postgres  # noqa: F401
from tests.test_migration_credentials import recovery_role  # noqa: F401
from tests.test_migration_tls import authority


def test_preview_configuration_discards_source_host_and_database(tmp_path):
    directory, tls, scratch = (tmp_path / name for name in ('recovery', 'tls', 'scratch'))
    (directory / 'catalog/files/config').mkdir(parents=True)
    tls.mkdir()
    (directory / 'catalog/files/config/local.json').write_text(json.dumps({
        'host': '0.0.0.0', 'database': {'user': 'old'}, 'source': {'root': 'old-source'},
        'recovery': {'activation_required': True, 'media_root': str(directory / 'media-copy/media')}}))
    (tls / 'TLS-REPORT.json').write_text(json.dumps({'ip_addresses': ['127.0.0.1']}))
    cfg = preview_configuration(directory, tls, scratch, {'user': 'preview'}, 4439)
    assert cfg['host'] == '127.0.0.1' and cfg['database'] == {'user': 'preview'}
    assert cfg['source'] == {} and cfg['read_only_preview'] is True
    assert cfg['destination']['root'] == str(directory / 'media-copy/media')
    assert cfg['recovery']['activation_required'] is True
    people = directory / 'catalog/files/data/people-preview.json'
    people.parent.mkdir()
    people.write_text(json.dumps({'enabled': False}))
    assert not preview_configuration(directory, tls, scratch, {}, 4439)['preview']['people_readonly']
    people.write_text(json.dumps({'enabled': True}))
    assert preview_configuration(directory, tls, scratch, {}, 4439)['preview']['people_readonly']


@pytest.mark.parametrize('field,value', [('host', '0.0.0.0'), ('port', 80), ('read_only_preview', False),
                                        ('limited_metadata_edit', True)])
def test_child_refuses_unsafe_preview_configuration(field, value):
    cfg = {'host': '127.0.0.1', 'port': 4439, 'read_only_preview': True,
           'recovery': {'activation_required': True},
           'database': {'options': '-c default_transaction_read_only=on'}, field: value}
    with pytest.raises(ValueError):
        check_configuration(cfg)


def test_actual_https_preview_child_serves_login_and_rejects_writes(recovery_role, tmp_path):
    run, params, record, role_directory = recovery_role
    run(preview=True)
    ca, tls, data, media, scratch = (tmp_path / name for name in ('ca', 'tls', 'data', 'media', 'scratch'))
    authority(ca)
    prepare_recovery_tls(ca, tls, ['127.0.0.1'])
    for path in (data, media, scratch, scratch / 'logs', scratch / 'temp'):
        path.mkdir()
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    with preview_database_access(admin, record, role_directory,
                                  expected_cluster_directory=Path(record['cluster_directory'])) as database:
        cfg = {'host': '127.0.0.1', 'port': port, 'read_only_preview': True, 'database': database,
               'recovery': {'activation_required': True}, 'destination': {'root': str(media)},
               'tls': {'certfile': str(tls / 'server.pem'), 'keyfile': str(tls / 'server-key.pem')},
               'preview': {'data_root': str(data), 'writable_root': str(scratch), 'people_readonly': False}}
        log = tmp_path / 'child.log'
        with log.open('wb') as output, subprocess.Popen(
                [sys.executable, '-B', '-m', 'app.migration.preview_server'],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                stdout=output, stderr=output) as process:
            try:
                process.stdin.write(json.dumps(cfg).encode())
                process.stdin.close()
                context = ssl.create_default_context(cafile=str(tls / 'ca.pem'))
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urllib.request.urlopen(f'https://127.0.0.1:{port}/login', context=context, timeout=1) as response:
                            assert response.status == 200
                            break
                    except (urllib.error.URLError, TimeoutError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            pytest.fail(log.read_text(errors='replace'))
                        time.sleep(.2)
                request = urllib.request.Request(f'https://127.0.0.1:{port}/api/albums', data=b'{}',
                                                 headers={'Content-Type': 'application/json'})
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, context=context, timeout=3)
                assert error.value.code == 403
                (scratch / 'STOP').touch()
                assert process.wait(timeout=15) == 0
                assert list(data.iterdir()) == [] and list(media.iterdir()) == []
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
