"""A permanently deleted photo's faces leave the People catalog. Real PostgreSQL, on the
opt-in disposable backup-plan-pg cluster only; every row is synthetic."""
import os
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from app.database.schema_upgrading import migrate
from app.library.face_cleanup import forget_faces, remove_face_thumbnails

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'c' * 32
SCHEMA = 'face_native_' + TOKEN
# The October migration's tables, as scripts/migrate_people_embeddings.py and
# finalize_people_migration.py made them.
MIGRATION_TABLES = '''
CREATE TABLE native_migration_faces(detection_id bigint PRIMARY KEY REFERENCES face_detections(id),
  model_id text NOT NULL, state text NOT NULL, result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE native_migration_photos(media_id bigint PRIMARY KEY REFERENCES media(id), model_id text NOT NULL,
  state text NOT NULL, detail text, seconds double precision NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE native_preserved_photos AS SELECT DISTINCT media_id, content_hash FROM face_analyses;
'''


@pytest.fixture
def catalog():
    port = os.environ.get('FAMILYFOTO_BACKUP_TEST_PORT')
    if not port:
        pytest.skip('Requires explicitly started disposable backup-plan-pg cluster')
    assert int(port) != 5432
    params = dict(host='127.0.0.1', port=int(port), user='backup_test')
    name = 'face_cleanup_' + uuid.uuid4().hex
    with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
        actual = Path(admin.execute('SHOW data_directory').fetchone()[0]).resolve()
        assert actual == (ROOT / 'temp/backup-plan-pg').resolve(), 'Not the disposable cluster'
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
    try:
        def connect(path):
            return lambda: psycopg.connect(**params, dbname=name, row_factory=dict_row,
                                           options='-c search_path=' + path)
        migrate(connect=connect('public'), directory=ROOT / 'databases', before_upgrade=None)
        with connect('public')() as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(SCHEMA)))
        # As face_detect/cli/prepare.py builds a People catalog.
        migrate(connect=connect(SCHEMA), directory=ROOT / 'databases', before_upgrade=None)
        with connect(SCHEMA)() as db:
            for path in ('automatic_people.sql', 'face_quality.sql', 'matching_policy.sql'):
                db.execute((ROOT / 'face_detect/storage' / path).read_text(encoding='utf-8'))
            seed(db)
            db.execute(MIGRATION_TABLES)
            db.execute("INSERT INTO native_migration_faces SELECT id,'native','embedded','{}' FROM face_detections")
            db.execute("INSERT INTO native_migration_photos SELECT id,'native','done',NULL,1 FROM media")
        yield connect
    finally:
        with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def seed(db):
    """Two photos in the catalog's shadow copy. Photo 1 has two faces, photo 2 one."""
    db.execute("INSERT INTO jobs(id,source_root,state) OVERRIDING SYSTEM VALUE VALUES(1,'catalog-import:1','completed')")
    for media_id in (1, 2):
        db.execute("INSERT INTO media(id,sha256,relative_path,name,kind,size) OVERRIDING SYSTEM VALUE "
                   "VALUES(%s,%s,%s,%s,'image',1)", (media_id, f'{media_id:064x}', f'p{media_id}.jpg', f'p{media_id}.jpg'))
        db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) "
                   "VALUES(1,%s,0,0,'imported',%s)", (f'catalog:{media_id}', media_id))
        db.execute("INSERT INTO face_review_events(media_id,actor,action) VALUES(%s,'fixture','seen')", (media_id,))
        db.execute('INSERT INTO face_preferences(media_id,actor) VALUES(%s,%s)', (media_id, 'fixture'))
    db.execute("INSERT INTO face_models VALUES('detector','{}')")
    db.execute("INSERT INTO people_models VALUES('native','{}')")
    run = db.execute("INSERT INTO face_runs(model_id,actor) VALUES('detector','fixture') RETURNING id").fetchone()['id']
    prun = db.execute("INSERT INTO people_runs(model_id,actor) VALUES('native','fixture') RETURNING id").fetchone()['id']
    person = db.execute("INSERT INTO people(name) VALUES('Synthetic') RETURNING id").fetchone()['id']
    group = db.execute('INSERT INTO auto_people_groups(person_id) VALUES(%s) RETURNING id', (person,)).fetchone()['id']
    faces = {}
    for media_id, count in ((1, 2), (2, 1)):
        analysis = db.execute("INSERT INTO face_analyses(media_id,content_hash,model_id,width,height,outcome) "
                              "VALUES(%s,%s,'detector',10,10,'faces') RETURNING id",
                              (media_id, f'{media_id:064x}')).fetchone()['id']
        db.execute('INSERT INTO auto_people_scan_results VALUES(%s,%s)', (analysis, '[]'))
        db.execute("INSERT INTO face_work(run_id,media_id,content_hash) VALUES(%s,%s,%s)", (run, media_id, f'{media_id:064x}'))
        for _ in range(count):
            did = db.execute("INSERT INTO face_detections(analysis_id,box,score) VALUES(%s,'{}',0.99) RETURNING id",
                             (analysis,)).fetchone()['id']
            faces.setdefault(media_id, []).append(did)
            db.execute("INSERT INTO people_embeddings VALUES(%s,'native','[1,0]')", (did,))
            db.execute('INSERT INTO people_assignments VALUES(%s,%s)', (did, person))
            db.execute("INSERT INTO auto_people_members VALUES(%s,%s,'manual','native')", (did, group))
            db.execute("INSERT INTO auto_people_observations VALUES(%s,'native',false)", (did,))
            db.execute('INSERT INTO auto_people_blocks VALUES(%s,%s)', (did, person))
            db.execute("INSERT INTO auto_people_face_quality(detection_id,policy,metrics,state) VALUES(%s,'p','{}','released')", (did,))
            db.execute("INSERT INTO face_ignore_events(detection_id,actor,action) VALUES(%s,'fixture','ignore')", (did,))
            db.execute("INSERT INTO people_work(run_id,detection_id,media_id,content_hash) VALUES(%s,%s,%s,%s)",
                       (prun, did, media_id, f'{media_id:064x}'))
    # A decision across the two photos, and a face of photo 2 superseded by one of photo 1.
    db.execute('INSERT INTO people_constraints VALUES(%s,%s)', (faces[1][0], faces[2][0]))
    db.execute('UPDATE face_detections SET superseded_by=%s WHERE id=%s', (faces[1][1], faces[2][0]))
    db.execute("INSERT INTO people_previews VALUES('token',0,'{}','fixture')")
    return faces


def counts(db, media_id):
    s = sql.Identifier(SCHEMA)
    by_media = {t: db.execute(sql.SQL('SELECT count(*) n FROM {}.{} WHERE media_id=%s').format(s, sql.Identifier(t)),
                              (media_id,)).fetchone()['n']
                for t in ('face_analyses', 'face_work', 'face_review_events', 'face_preferences', 'people_work',
                          'import_items', 'native_migration_photos', 'native_preserved_photos')}
    by_media['media'] = db.execute(sql.SQL('SELECT count(*) n FROM {}.media WHERE id=%s').format(s), (media_id,)).fetchone()['n']
    return by_media


def test_a_permanently_deleted_photo_leaves_no_people_data(catalog, tmp_path):
    with catalog('public')() as db:
        removed = forget_faces(db, 1)
        # The caller's search path is back, so the rest of its transaction is unaffected.
        assert db.execute("SELECT current_setting('search_path') p").fetchone()['p'] == 'public'
    assert list(removed) == [SCHEMA] and len(removed[SCHEMA]) == 2
    with catalog(SCHEMA)() as db:
        assert set(counts(db, 1).values()) == {0}
        assert set(counts(db, 2).values()) == {1}
        for table in ('face_detections', 'people_embeddings', 'people_assignments', 'auto_people_members',
                      'auto_people_observations', 'auto_people_blocks', 'auto_people_face_quality',
                      'face_ignore_events', 'native_migration_faces'):
            assert db.execute(sql.SQL('SELECT count(*) n FROM {}').format(sql.Identifier(table))).fetchone()['n'] == 1, table
        # The cross-photo decision went with the face; the superseded link was cleared.
        assert db.execute('SELECT count(*) n FROM people_constraints').fetchone()['n'] == 0
        assert db.execute('SELECT superseded_by FROM face_detections').fetchone()['superseded_by'] is None
        # The catalog's own trigger ran against the catalog, not the main schema.
        assert db.execute('SELECT count(*) n FROM people_previews').fetchone()['n'] == 0
        assert db.execute('SELECT revision FROM people_revision').fetchone()['revision'] > 0
        # The person and the group stay; only the faces went.
        assert db.execute('SELECT count(*) n FROM people').fetchone()['n'] == 1
        assert db.execute('SELECT count(*) n FROM auto_people_groups').fetchone()['n'] == 1
    with catalog('public')() as db:
        assert db.execute('SELECT revision FROM public.people_revision').fetchone()['revision'] == 0

    thumbnails = tmp_path / 'face_detect/profiles' / TOKEN / 'thumbnails'
    thumbnails.mkdir(parents=True)
    for did in (*removed[SCHEMA], 999):
        (thumbnails / f'{did}.jpg').write_bytes(b'face')
    remove_face_thumbnails(tmp_path, removed)
    assert [p.name for p in thumbnails.iterdir()] == ['999.jpg']


def test_the_one_off_script_cleans_only_photos_whose_deletion_finished(catalog, tmp_path, capsys, monkeypatch):
    import importlib.util
    import json
    # The script sets FAMILYFOTO_DATA_DIR for its own run; restore it for later tests.
    monkeypatch.delenv('FAMILYFOTO_DATA_DIR', raising=False)
    monkeypatch.setenv('FAMILYFOTO_DATA_DIR', str(tmp_path / 'data'))
    spec = importlib.util.spec_from_file_location('forget_script', ROOT / 'scripts/forget_deleted_photo_faces.py')
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    with catalog('public')() as db:
        for media_id in (1, 2):
            db.execute("INSERT INTO media(id,sha256,relative_path,name,kind,size) OVERRIDING SYSTEM VALUE "
                       "VALUES(%s,%s,%s,%s,'image',1)", (media_id, f'{media_id:064x}', f'p{media_id}.jpg', f'p{media_id}.jpg'))
        # Photo 1 was deleted for good before the fix; photo 2 is only in Trash.
        db.execute('INSERT INTO media_trash(media_id,deleted_at) VALUES(1,now()),(2,NULL)')
        info = db.info
        database = {'host': info.host, 'port': info.port, 'user': info.user, 'dbname': info.dbname}
    config = tmp_path / 'local.json'
    config.write_text(json.dumps({'database': database}))
    data = tmp_path / 'data'
    thumbnails = data / 'face_detect/profiles' / TOKEN / 'thumbnails'
    thumbnails.mkdir(parents=True)
    with catalog(SCHEMA)() as db:
        ids = [r['id'] for r in db.execute('SELECT id FROM face_detections ORDER BY id')]
    for did in ids:
        (thumbnails / f'{did}.jpg').write_bytes(b'face')

    assert script.main(['--config', str(config), '--data', str(data)]) == 0
    found = json.loads(capsys.readouterr().out)['found']
    assert list(found.values()) == [{'photos': 1, 'faces': 2, 'thumbnail_folder_present': True}]
    with catalog(SCHEMA)() as db:
        assert counts(db, 1)['face_analyses'] == 1, 'A report changes nothing'

    assert script.main(['--config', str(config), '--data', str(data), '--apply']) == 0
    out = capsys.readouterr().out
    result = json.loads(out[out.index('\n}\n{') + 3:])  # after the report printed first
    assert result['removed'] == {'photos': 1, 'faces': 2}
    assert list(result['left'].values())[0]['photos'] == 0
    with catalog(SCHEMA)() as db:
        assert set(counts(db, 1).values()) == {0}
        assert set(counts(db, 2).values()) == {1}
    assert sorted(p.name for p in thumbnails.iterdir()) == [f'{ids[-1]}.jpg']


def test_a_photo_with_no_faces_and_a_failed_delete_change_nothing(catalog):
    with catalog(SCHEMA)() as db:
        before = counts(db, 1)
    assert all(before.values())
    with catalog('public')() as db:
        assert forget_faces(db, 12345) == {SCHEMA: []}
    with catalog('public')() as db:
        forget_faces(db, 1)
        db.rollback()
    with catalog(SCHEMA)() as db:
        assert counts(db, 1) == before
        assert db.execute('SELECT count(*) n FROM face_detections').fetchone()['n'] == 3
