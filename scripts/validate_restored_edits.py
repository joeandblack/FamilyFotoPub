"""Exercise real HTTP edit handlers against a restored, localhost-only database."""
import argparse
from contextlib import asynccontextmanager, contextmanager
import copy
import json
from pathlib import Path
import secrets
import sys
import tempfile
import time

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--limited-role', action='store_true')
    parser.add_argument('--native-migration', type=Path)
    args = parser.parse_args()
    backup = args.backup.resolve()
    manifest = json.loads((backup/'manifest.json').read_text())
    if not manifest['stages'].get('backup_complete'):
        raise RuntimeError('A verified backup and restore are required first')
    settings = json.loads((backup/'validation-connection.json').read_text())
    if (settings['host'], settings['port'], settings['dbname']) != ('127.0.0.1', 55449, 'familyfoto_validation'):
        raise RuntimeError('Refusing to test edits outside the isolated validation database')
    with psycopg.connect(**settings) as db:
        if db.execute('SHOW data_directory').fetchone()[0].replace('\\', '/') != str(backup/'validation-postgres').replace('\\', '/'):
            raise RuntimeError('Unexpected validation cluster directory')
    if args.limited_role:
        settings = json.loads((backup/'limited-edit-test.json').read_text())['database']
        from app.jobs.limited_edit import verify_database
        verify_database(json.loads((backup/'limited-edit-test.json').read_text()))
        for statement in [
            'DELETE FROM public.media WHERE false',
            'UPDATE public.media SET relative_path=relative_path WHERE false',
            'UPDATE public.media SET sha256=sha256 WHERE false',
            'DELETE FROM public.jobs WHERE false',
            'UPDATE public.media_trash SET deleted_at=now() WHERE false',
        ]:
            with psycopg.connect(**settings) as db:
                try:
                    db.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    pass
                else:
                    raise RuntimeError('Forbidden operation was permitted: '+statement)

    from app import config
    config.DATA = backup/'test-state'
    config.LOGS = config.DATA/'logs'
    config.TEMP = config.DATA/'temp'
    for folder in (config.DATA, config.LOGS, config.TEMP, backup/'empty-media'):
        folder.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(config.TEMP)
    cfg = {'database': settings, 'destination': {'root': str(backup/'empty-media')},
           'source': {}, 'browser_user': 'validation', 'auth_secret': secrets.token_urlsafe(40),
           'automatic_people': {'enabled': False}, 'host': '127.0.0.1', 'port': 55450}
    from app.auth import password_hash
    cfg['browser_password_hash'] = password_hash(secrets.token_urlsafe(40))
    config.load_config = lambda: copy.deepcopy(cfg)
    config.initialize_runtime = lambda: None
    from app.service.readonly_preview import MemorySessions, install_write_guard
    # No test may modify the real checkout, source profile, or destination disk.
    install_write_guard(backup)
    from app import auth
    auth.SessionStore = MemorySessions
    from app import api, database
    from app.auth import api as auth_api
    database.load_config = lambda: copy.deepcopy(cfg)
    auth_api.SessionStore = MemorySessions

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            database.close_pools()

    api.app.router.lifespan_context = lifespan
    from app.database import schema_upgrading
    upgraded = [] if args.limited_role else schema_upgrading.migrate()
    from face_detect.api.readonly_people import catalog_settings
    catalog = (json.loads((args.native_migration/'native-runtime.json').read_text())
               if args.native_migration else catalog_settings(backup/'installation/data'))
    people_settings = {**settings, 'options': '-c search_path='+catalog['schema']}

    @contextmanager
    def people_connection():
        with psycopg.connect(**people_settings, row_factory=dict_row) as db:
            yield db

    from face_detect.api import automatic_people_api as people_api
    from face_detect.recognition import automatic_people as service
    from face_detect.recognition import people
    service.load_config = lambda: {'automatic_people': {'enabled': True}}
    people_api.connection = people_connection
    api.app.include_router(people_api.router)
    from fastapi.testclient import TestClient
    report = {'schema_upgrades': [getattr(item, 'version', item) for item in upgraded],
              'database': settings['dbname'], 'port': settings['port'], 'checks': []}
    report_path = (args.native_migration/'native-write-validation.json' if args.native_migration else
                   backup/('limited-role-validation.json' if args.limited_role else 'write-validation.json'))

    def passed(name):
        report['checks'].append(name)
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('PASS: '+name, flush=True)

    def revision():
        with people_connection() as db:
            return people.revision(db)

    with TestClient(api.app) as client:
        client.cookies.set('familyfoto_session', auth.session_token(cfg, int(time.time())+3600))

        def call(method, path, data=None, status=200):
            response = client.request(method, path, json=data)
            if response.status_code != status:
                raise AssertionError(f'{method} {path}: {response.status_code}: {response.text[:400]}')
            return response.json()

        cards = call('GET', '/api/automatic-people/cards')['groups']
        group = call('GET', f"/api/automatic-people/groups/{cards[0]['id']}")['group']
        pid, gid = group['person_id'], group['id']
        rename_path = f'/api/automatic-people/people/{pid}/rename'
        test_name = 'Validation temporary name '+secrets.token_hex(4)
        call('POST', rename_path, {'revision': revision(), 'name': test_name, 'expected_name': group['name']})
        assert call('GET', f'/api/automatic-people/groups/{gid}')['group']['name'] == test_name
        call('POST', rename_path, {'revision': revision(), 'name': group['name'], 'expected_name': test_name})
        passed('Rename and restore existing person across committed requests')

        face = next(face for face in group['faces'] if face['detection_id'] != group['cover_detection_id'])
        did = face['detection_id']
        call('POST', f'/api/automatic-people/groups/{gid}/cover',
             {'revision': revision(), 'detection_id': did, 'expected_cover_id': group['cover_detection_id']})
        assert call('GET', f'/api/automatic-people/groups/{gid}')['group']['cover_detection_id'] == did
        passed('Change person cover and reload')

        for enabled in (True, False):
            call('POST', f'/api/automatic-people/faces/{did}/ignored',
                 {'revision': revision(), 'enabled': enabled})
            with people_connection() as db:
                assert db.execute('SELECT ignored FROM face_detections WHERE id=%s', (did,)).fetchone()['ignored'] == enabled
        passed('Ignore and restore existing face')

        review = call('GET', '/api/automatic-people/review-page?page_size=1')['groups'][0]
        member = review['faces'][0]['detection_id']
        with people_connection() as db:
            before = db.execute('SELECT * FROM auto_people_members WHERE detection_id=%s', (member,)).fetchone()
        changed = call('POST', f'/api/automatic-people/faces/{member}/correct',
                       {'revision': revision(), 'person_id': pid})
        with people_connection() as db:
            assert db.execute('SELECT person_id FROM people_assignments WHERE detection_id=%s', (member,)).fetchone()['person_id'] == pid
        call('POST', f"/api/automatic-people/events/{changed['event_id']}/undo", {'revision': revision()})
        with people_connection() as db:
            assert db.execute('SELECT * FROM auto_people_members WHERE detection_id=%s', (member,)).fetchone() == before
        passed('Match existing unassigned face and Undo restores exact membership')

        call('POST', f"/api/automatic-people/groups/{review['id']}/selected-faces",
             {'revision': revision(), 'detection_ids': [member], 'action': 'move', 'person_id': pid})
        with people_connection() as db:
            event_id = db.execute("SELECT max(id) id FROM auto_people_events WHERE action='correct'").fetchone()['id']
        call('POST', f'/api/automatic-people/events/{event_id}/undo', {'revision': revision()})
        passed('Selected-face Move to person and Undo')

        with database.connection() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM media m WHERE NOT EXISTS '
                   '(SELECT 1 FROM media_trash t WHERE t.media_id=m.id) ORDER BY id LIMIT 3').fetchall()]
        album = call('POST', '/api/albums', {'name': 'Validation album', 'media_ids': ids})
        aid = album['id']
        assert album['item_count'] == 3
        assert call('POST', f'/api/albums/{aid}/items', {'media_ids': ids})['added'] == 0
        call('PATCH', f'/api/albums/{aid}', {'name': 'Validation renamed', 'description': 'Isolated test only'})
        call('POST', f'/api/albums/{aid}/items/{ids[1]}/cover')
        assert call('GET', f'/api/albums/{aid}')['cover_id'] == ids[1]
        call('POST', f'/api/albums/{aid}/remove', {'media_ids': [ids[0]]})
        assert call('GET', f'/api/albums/{aid}')['item_count'] == 2
        target = call('POST', '/api/albums', {'name': 'Validation target', 'media_ids': []})['id']
        call('POST', f'/api/albums/{aid}/transfer', {'target_id': target, 'media_ids': [ids[1]]})
        assert call('GET', f'/api/albums/{target}')['item_count'] == 1
        collection = call('POST', '/api/album-collections', {'name': 'Validation collection'})['id']
        child = call('POST', '/api/album-collections', {'name': 'Validation child', 'parent_id': collection})['id']
        assert call('POST', f'/api/album-collections/{collection}/albums', {'album_ids': [aid]})['added'] == 1
        assert call('POST', f'/api/album-collections/{collection}/albums', {'album_ids': [aid]})['added'] == 0
        call('POST', f'/api/album-collections/{collection}/parent', {'parent_id': child}, status=409)
        call('PATCH', f'/api/album-collections/{child}', {'name': 'Validation child renamed'})
        call('POST', f'/api/album-collections/{collection}/remove', {'album_ids': [aid]})
        call('DELETE', f'/api/album-collections/{collection}')
        assert call('GET', f'/api/albums/{aid}')['item_count'] == 1
        call('DELETE', f'/api/album-collections/{child}')
        passed('Collection create/edit/membership/delete and cycle rejection preserve albums')
        call('DELETE', f'/api/albums/{aid}')
        call('DELETE', f'/api/albums/{target}')
        with database.connection() as db:
            assert db.execute('SELECT count(*) n FROM media WHERE id=ANY(%s)', (ids,)).fetchone()['n'] == 3
        passed('Album create/edit/cover/add/remove/transfer/delete preserve original media records')
    report['complete'] = True
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
