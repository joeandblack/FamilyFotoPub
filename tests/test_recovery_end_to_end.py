"""A whole recovery on the real schema and real servers: back up, restore, preview, activate, start, repair, plan the next backup."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import http.cookiejar

from PIL import Image
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from app.auth import password_hash
from app.backup.library import capture_library
from app.backup.library_restore import restore_library
from app.backup.postgres import Endpoint
from app.database.schema_upgrading import migrate
from app.migration.activation import ACCEPTANCE_CHECKS, prepare_activation
from app.migration.activation_commit import commit_activation
from app.migration.credentials import prepare_recovery_role
from app.migration.disk import disk_binding
from app.migration.preview import preview_library
from app.migration.tls import prepare_recovery_tls
from tests.test_backup_postgres import ample, postgres  # noqa: F401
from tests.test_migration_tls import authority
from tests.backup_keys import backup_key

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = ROOT / 'temp/backup-plan-pg'
PASSWORD = 'fixture-admin-password'
NAME = 'été 照片.jpg'


def photo():
    stream = io.BytesIO()
    Image.new('RGB', (640, 480), (20, 70, 130)).save(stream, format='JPEG')
    return stream.getvalue()


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Browser:
    def __init__(self, port, ca):
        context = ssl.create_default_context(cafile=str(ca))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                                  urllib.request.HTTPSHandler(context=context))
        self.base = f'https://127.0.0.1:{port}'

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def wait(self, process, log):
        deadline = time.monotonic() + 60
        while True:
            try:
                if self.request('/login')[0] == 200:
                    return
            except (urllib.error.URLError, OSError):
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail(Path(log).read_text(errors='replace')[-3000:])
            time.sleep(.3)

    def sign_in(self):
        assert self.request('/api/login', {'username': 'family', 'password': PASSWORD})[0] == 200

    def json(self, path, body=None):
        status, payload = self.request(path, body)
        assert status == 200, (path, status, payload[:500])
        return json.loads(payload)


def read_library(browser, content):
    items = browser.json('/api/media')['items']
    assert [item['name'] for item in items] == [NAME]
    media = items[0]['id']
    assert browser.request(f'/api/media/{media}/original') == (200, content)
    assert [album['name'] for album in browser.json('/api/albums')['albums']] == ['Summer']
    assert browser.json('/api/media?album=1')['total'] == 1
    assert browser.json('/api/media?kind=trash')['total'] == 1
    return media


@pytest.fixture(params=['copied', 'in-place', 'split'])
def world(postgres, request):  # noqa: F811
    in_place = request.param != 'copied'
    params, source, _, dump, restore = postgres
    # Short and resolved: job folders run near the Windows path limit, and macOS's temp folder is under a symlink.
    base = Path(tempfile.mkdtemp(prefix='ff-e2e-')).resolve()
    config, data, media = base / 'old/config', base / 'old/data', base / 'old/media'
    for path in (config, data, media):
        path.mkdir(parents=True)
    authority(config / 'tls')
    content = photo()
    (media / 'trip').mkdir()
    (media / 'trip' / NAME).write_bytes(content)
    (media / 'deleted.jpg').write_bytes(content + b'\xff')
    settings = {'host': '127.0.0.1', 'port': 4340, 'browser_user': 'family', 'browser_password_hash': password_hash(PASSWORD),
                'auth_secret': 'a' * 43, 'backup': {'pg_dump': str(dump)}, 'source': {}, 'destination': disk_binding(media), 'io_mibps': 500,
                'database': {'host': params['host'], 'port': params['port'], 'dbname': source, 'user': params['user']}}
    (config / 'local.json').write_text(json.dumps(settings), encoding='utf-8')
    database = {**params, 'dbname': source}

    def connect():
        return psycopg.connect(**database, row_factory=dict_row)
    migrate(connect=connect, directory=ROOT / 'databases')
    with psycopg.connect(**database) as db:
        rows = [('trip/' + NAME, NAME, content), ('deleted.jpg', 'deleted.jpg', content + b'\xff')]
        ids = [db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,width,height,visibility) "
                          "VALUES(%s,%s,%s,'image',%s,640,480,'shared') RETURNING id",
                          (hashlib.sha256(body).hexdigest(), path, name, len(body))).fetchone()[0] for path, name, body in rows]
        album = db.execute("INSERT INTO albums(name) VALUES('Summer') RETURNING id").fetchone()[0]
        db.execute('INSERT INTO album_items(album_id,media_id,position) VALUES(%s,%s,0)', (album, ids[0]))
        db.execute("INSERT INTO media_trash(media_id,deleted_by) VALUES(%s,'family')", (ids[1],))
    key = backup_key(PASSWORD)  # The backup password is the administrator password.
    backup, target = base / 'backup', base / 'r'
    capture_library(Endpoint(params['host'], params['port'], source, params['user']), dump, config, data, media, backup, key,
                    database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample)
    shutil.move(base / 'old', base / 'lost')  # The original is gone from here on.
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    report = restore_library(backup, key, target, admin, restore, expected_cluster_directory=CLUSTER,
                             database_budget=1024 ** 2, guard_factory=ample, in_place=in_place,
                             media_directory=base / 'm' if request.param == 'split' else None)
    # In place, the backup's own media becomes the recovered library's, and the backup is used up.
    assert report['media_in_place'] is in_place
    assert (backup / 'media-copy').exists() is not in_place
    assert (backup / 'MEDIA-MOVED.json').exists() is in_place
    # Split, the media has its own folder and the recovery folder keeps only the private part.
    assert (target / 'media-copy').exists() is (request.param != 'split')
    assert report['media_root'] == str((base / 'm' if request.param == 'split' else target) / 'media-copy/media')
    record = json.loads((target / 'catalog/database-target.json').read_text())
    roles = [prepare_recovery_role(admin, record, base / name, expected_cluster_directory=CLUSTER, preview=preview)['role']
             for name, preview in (('role-preview', True), ('role-write', False))]
    prepare_recovery_tls(target / 'catalog/files/config/tls', base / 'tls', ['127.0.0.1'])
    environment = {name: value for name, value in os.environ.items() if not name.startswith(('FAMILYFOTO_', 'PG'))}
    try:
        yield base, backup, key, target, admin, content, environment
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(report['database'])))
            for role in roles:
                db.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
        shutil.rmtree(base, ignore_errors=True)


def test_a_lost_library_comes_back_and_can_be_backed_up_again(world):
    base, backup, key, target, admin, content, environment = world
    from app.migration.move_in import restored_password_matches
    assert restored_password_matches(target, PASSWORD)  # The password that opened it signs in.
    data = target / 'catalog/files/data'
    ca = base / 'tls/ca.pem'
    seen_before = sorted(path.name for path in data.iterdir())

    # The preview shows the library from the recovered copy without writing to it.
    def launch(cfg):
        log = base / 'preview.log'
        with log.open('wb') as output, subprocess.Popen([sys.executable, '-B', '-m', 'app.migration.preview_server'], cwd=ROOT,
                                                        env=environment, stdin=subprocess.PIPE, stdout=output, stderr=output) as process:
            try:
                process.stdin.write(json.dumps(cfg).encode())
                process.stdin.close()
                browser = Browser(cfg['port'], ca)
                browser.wait(process, log)
                browser.sign_in()
                media = read_library(browser, content)
                status, thumbnail = browser.request(f'/api/media/{media}/thumbnail')
                assert status == 200 and thumbnail[:2] == b'\xff\xd8'
                assert browser.request(f'/api/media/{media}/rotate', {})[0] == 403
                (Path(cfg['preview']['writable_root']) / 'STOP').touch()
                assert process.wait(timeout=30) == 0
            finally:
                if process.poll() is None:
                    process.kill()
    assert preview_library(backup, key, target, admin, base / 'role-preview', base / 'tls', base / 'scratch',
                           expected_cluster_directory=CLUSTER, port=free_port(), launch=launch)['state'] == 'preview-closed'
    assert sorted(path.name for path in data.iterdir()) == seen_before

    # Activating makes it the real application.
    port = free_port()
    intent = base / 'intent'
    prepare_activation(backup, key, target, admin, base / 'role-write', base / 'tls', intent, expected_cluster_directory=CLUSTER,
                       accepted=ACCEPTANCE_CHECKS, source_unavailable=True, hostname='127.0.0.1', port=port, guard_factory=ample)
    assert commit_activation(intent, key, admin, offline_confirmed=True, guard_factory=ample)['state'] == 'activated'
    log = base / 'app.log'
    with log.open('wb') as output, subprocess.Popen([sys.executable, '-m', 'app.migration.launch', '--directory', str(target)], cwd=ROOT,
                                                    env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=output) as process:
        try:
            browser = Browser(port, ca)
            browser.wait(process, log)
            browser.sign_in()
            media = read_library(browser, content)
            # A backup leaves thumbnails out, so a repair puts them back, and its report can't block the next backup.
            assert browser.request(f'/api/media/{media}/thumbnail')[0] == 404
            repair = subprocess.run([sys.executable, '-m', 'app.migration.launch', '--directory', str(target), '--repair-thumbnails'],
                                    cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
            assert repair.returncode == 0, repair.stdout + repair.stderr
            status, thumbnail = browser.request(f'/api/media/{media}/thumbnail')
            assert status == 200 and thumbnail[:2] == b'\xff\xd8'
            assert list(data.glob('thumbnail-repair-*.json'))
            # The recovered library is a normal one now: it changes, and its backup page can plan the next backup.
            assert browser.json('/api/albums', {'name': 'After recovery'})['name'] == 'After recovery'
            page = browser.json('/api/backup')
            if sys.platform == 'win32':
                assert page['unsupported'] is None
            destination = base / 'next-backup'
            destination.mkdir()
            # The same password opened the backup and signs in, and protects the next backup.
            plan = browser.json('/api/backup/plan', {'destination': str(destination), 'password': PASSWORD})
            assert plan['problems'] == []
            wrong = browser.json('/api/backup/plan', {'destination': str(destination), 'password': PASSWORD + '!'})
            assert any('not the FamilyFoto administrator password' in problem for problem in wrong['problems'])
            (data / 'DESKTOP-SERVER-STOP').touch()
            assert process.wait(timeout=60) == 0
        finally:
            if process.poll() is None:
                process.kill()
