"""Migration of a per-model People catalog into `people`. Real PostgreSQL, on the opt-in
disposable backup-plan-pg cluster only; every row and manifest is synthetic."""
import hashlib
import json
import os
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from app.backup.policy import UnclassifiedState, check_directory, classify_file, classify_table
from app.database.schema_upgrading import migrate as migrate_app
from face_detect.catalog import migrate as catalog
from face_detect.spaces.identity import detector_id, space_id
from tests.backup_keys import backup_key

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'd' * 32
SOURCE = 'face_native_' + TOKEN
NATIVE = {'schema': 'familyfoto-native-model-v1', 'provider': 'insightface',
          'assets': {'recognizer': {'sha256': 'a' * 64}, 'detector': {'sha256': 'b' * 64}},
          'preprocessing': {'decode': 'canonical-v1'}, 'dimensions': 2, 'normalization': 'l2',
          'settings': {'detection_threshold': 0.5}, 'device': 'cpu',
          'runtime_versions': {'insightface': '2.1', 'onnxruntime': '1.31.1'},
          'matching': {'mode': 'review-only', 'calibrated': False}}
LEGACY = {'provider': 'compreface', 'dimensions': 2, 'normalization': 'l2', 'preprocessing': 'x', 'threshold': 0.4}


@pytest.fixture
def cluster():
    port = os.environ.get('FAMILYFOTO_BACKUP_TEST_PORT')
    if not port:
        pytest.skip('Requires explicitly started disposable backup-plan-pg cluster')
    assert int(port) != 5432
    params = dict(host='127.0.0.1', port=int(port), user='backup_test')
    with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
        actual = Path(admin.execute('SHOW data_directory').fetchone()[0]).resolve()
        assert actual == (ROOT / 'temp/backup-plan-pg').resolve(), 'Not the disposable cluster'
    created = []

    def library():
        """A new database holding a main catalog and one per-model People catalog."""
        name = 'catalog_migration_' + uuid.uuid4().hex
        with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        created.append(name)

        def connect(path='public'):
            return psycopg.connect(**params, dbname=name, row_factory=dict_row, options='-c search_path=' + path)
        migrate_app(connect=lambda: connect('public'), directory=ROOT / 'databases', before_upgrade=None)
        with connect() as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(SOURCE)))
        migrate_app(connect=lambda: connect(SOURCE), directory=ROOT / 'databases', before_upgrade=None)
        with connect(SOURCE) as db:
            for path in ('automatic_people.sql', 'face_quality.sql', 'matching_policy.sql'):
                db.execute((ROOT / 'face_detect/storage' / path).read_text(encoding='utf-8'))
            db.execute('CREATE TABLE native_catalog_state(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), '
                       'model_id text NOT NULL REFERENCES people_models(id), legacy_pilot text NOT NULL)')
            db.execute('CREATE TABLE native_preserved_photos(media_id bigint, content_hash text)')
        with connect() as db:
            seed(db)
        return name, connect

    yield params, library
    with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
        for name in created:
            admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))


def seed(db):
    """Three photos. Photo 1 was scanned by the October model, 2 and 3 by the live one.
    Photo 3 has two faces with the same box."""
    s = sql.Identifier(SOURCE)

    def q(text, args=()):
        return db.execute(sql.SQL(text).format(s=s), args)
    q("INSERT INTO {s}.jobs(id,source_root,state) OVERRIDING SYSTEM VALUE VALUES(1,'catalog-import:1','completed')")
    for media_id in (1, 2, 3):
        values = (media_id, f'{media_id:064x}', f'p{media_id}.jpg', f'p{media_id}.jpg')
        db.execute("INSERT INTO public.media(id,sha256,relative_path,name,kind,size) OVERRIDING SYSTEM VALUE "
                   "VALUES(%s,%s,%s,%s,'image',1)", values)
        q("INSERT INTO {s}.media(id,sha256,relative_path,name,kind,size) OVERRIDING SYSTEM VALUE "
          "VALUES(%s,%s,%s,%s,'image',1)", values)
        q("INSERT INTO {s}.face_review_events(media_id,actor,action) VALUES(%s,'fixture','seen')", (media_id,))
    for model, manifest in (('native-model', NATIVE), ('legacy-model', LEGACY)):
        q('INSERT INTO {s}.face_models(id,manifest) VALUES(%s,%s)', (model, json.dumps(manifest)))
        q('INSERT INTO {s}.people_models VALUES(%s,%s)', (model, json.dumps(manifest)))
    q("INSERT INTO {s}.native_catalog_state VALUES(true,'native-model','legacy-pilot')")
    q("INSERT INTO {s}.native_matching_policies(model_id,policy,evidence) VALUES('native-model','{{\"threshold\":0.7}}','{{\"qualified\":true}}')")
    run = q("INSERT INTO {s}.face_runs(model_id,actor) VALUES('native-model','fixture') RETURNING id").fetchone()['id']
    prun = q("INSERT INTO {s}.people_runs(model_id,actor) VALUES('native-model','fixture') RETURNING id").fetchone()['id']
    person = q("INSERT INTO {s}.people(name) VALUES('Synthetic') RETURNING id").fetchone()['id']
    group = q('INSERT INTO {s}.auto_people_groups(person_id) VALUES(%s) RETURNING id', (person,)).fetchone()['id']
    box = {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4}
    faces = []
    for media_id, model, count in ((1, 'legacy-model', 1), (2, 'native-model', 1), (3, 'native-model', 2)):
        analysis = q("INSERT INTO {s}.face_analyses(media_id,content_hash,model_id,width,height,outcome,run_id) "
                     "VALUES(%s,%s,%s,10,10,'faces',%s) RETURNING id",
                     (media_id, f'{media_id:064x}', model, run)).fetchone()['id']
        q('INSERT INTO {s}.auto_people_scan_results VALUES(%s,%s)', (analysis, '[]'))
        q('INSERT INTO {s}.face_work(run_id,media_id,content_hash) VALUES(%s,%s,%s)', (run, media_id, f'{media_id:064x}'))
        for _ in range(count):
            did = q("INSERT INTO {s}.face_detections(analysis_id,box,score,landmark_schema) VALUES(%s,%s,0.99,'five-point-v1') "
                    "RETURNING id", (analysis, json.dumps(box))).fetchone()['id']
            faces.append(did)
            q("INSERT INTO {s}.people_embeddings VALUES(%s,'legacy-model','[1,0]')", (did,))
            if model == 'native-model':
                q("INSERT INTO {s}.people_embeddings VALUES(%s,'native-model','[0,1]')", (did,))
                q("INSERT INTO {s}.auto_people_observations VALUES(%s,'native-model',true)", (did,))
            q("INSERT INTO {s}.auto_people_observations VALUES(%s,'legacy-model',false)", (did,))
            q('INSERT INTO {s}.people_assignments VALUES(%s,%s)', (did, person))
            q("INSERT INTO {s}.auto_people_members VALUES(%s,%s,'manual','native-model')", (did, group))
            q('INSERT INTO {s}.auto_people_blocks VALUES(%s,%s)', (did, person))
            q("INSERT INTO {s}.auto_people_face_quality(detection_id,policy,metrics,state) VALUES(%s,'p','{{}}','released')", (did,))
            q("INSERT INTO {s}.face_ignore_events(detection_id,actor,action) VALUES(%s,'fixture','ignore')", (did,))
            q('INSERT INTO {s}.people_work(run_id,detection_id,media_id,content_hash) VALUES(%s,%s,%s,%s)',
              (prun, did, media_id, f'{media_id:064x}'))
            q('INSERT INTO {s}.face_redetect_targets VALUES(%s,%s)', (run, did))
    q('INSERT INTO {s}.people_constraints VALUES(%s,%s)', (faces[0], faces[1]))
    # A face superseded by a later one.
    q('UPDATE {s}.face_detections SET superseded_by=%s WHERE id=%s', (faces[3], faces[0]))
    q("INSERT INTO {s}.people_suggestions(run_id,model_id,detection_ids,min_similarity) VALUES(%s,'native-model',%s,0.8)",
      (prun, json.dumps(faces[1:3])))
    q("INSERT INTO {s}.auto_people_receipts VALUES('r1','h','native-model','{{}}'),('r0','h','legacy-model','{{}}')")
    q("INSERT INTO {s}.auto_people_events(action,origin,actor,before_state,after_revision) VALUES('correct','manual','fixture',%s,3)",
      (json.dumps({'member': {'detection_id': faces[1]}}),))
    q("INSERT INTO {s}.people_events(action,actor,before_state,after_revision) VALUES('assign','fixture','{{}}',2)")
    q("INSERT INTO {s}.people_lifecycle_events(person_id,action,actor,old_name,new_name) VALUES(%s,'restore','fixture','a','b')",
      (person,))
    q("INSERT INTO {s}.people_previews VALUES('token',0,'{{}}','fixture')")
    q('INSERT INTO {s}.native_preserved_photos VALUES(1,%s)', (f'{1:064x}',))
    q('UPDATE {s}.people_revision SET revision=7')


def fingerprint(db, schema):
    """Every table of a schema: row count and a digest of its sorted rows."""
    out = {}
    for row in db.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY 1', (schema,)).fetchall():
        rows = db.execute(sql.SQL('SELECT to_jsonb(t)::text j FROM {}.{} t ORDER BY 1').format(
            sql.Identifier(schema), sql.Identifier(row['tablename']))).fetchall()
        out[row['tablename']] = (len(rows), hashlib.sha256('\n'.join(r['j'] for r in rows).encode()).hexdigest())
    return out


def run_migration(connect):
    with connect() as db:
        report = catalog.migrate(db, SOURCE)
        db.commit()
    return report


def test_a_catalog_moves_into_people_with_ids_kept_and_keys_mapped(cluster):
    _, library = cluster
    _, connect = library()
    with connect() as db:
        before = fingerprint(db, SOURCE)
        public = fingerprint(db, 'public')
    report = run_migration(connect)
    live, legacy = space_id(NATIVE), 'legacy-legacy-model'
    assert report['active_space_id'] == live and report['spaces'] == {live: 'active', legacy: 'retired'}
    with connect() as db:
        # Nothing in the source or the main catalog changed.
        assert fingerprint(db, SOURCE) == before and fingerprint(db, 'public') == public
        source_counts = {t: n for t, (n, _) in before.items()}
        for table in ('face_analyses', 'face_detections', 'people', 'people_assignments', 'auto_people_members',
                      'auto_people_events', 'face_ignore_events', 'people_constraints', 'auto_people_receipts'):
            assert report['tables'][table] == source_counts[table], table
        assert report['tables']['embedding'] == source_counts['people_embeddings']
        # Keys mapped: detectors, spaces, the live policy, the catalog state.
        assert {r['detector_id'] for r in db.execute('SELECT DISTINCT detector_id FROM people.face_analyses')} == {
            detector_id(NATIVE), legacy}
        assert {r['space_id'] for r in db.execute('SELECT DISTINCT space_id FROM people.auto_people_members')} == {live}
        policy = db.execute('SELECT * FROM people.policy').fetchone()
        assert policy['space_id'] == live and policy['policy'] == {'threshold': 0.7}
        state = db.execute('SELECT * FROM people.catalog_state').fetchone()
        assert state['active_space_id'] == live and state['migrated_from'] == SOURCE and state['legacy_pilot'] == 'legacy-pilot'
        assert db.execute('SELECT revision FROM people.people_revision').fetchone()['revision'] == 7
        # Live vectors point at an origin record, which never counts as a passed check.
        origin = db.execute('SELECT * FROM people.certificate').fetchone()
        assert origin['reference'] == 'origin' and origin['passed'] is False
        assert origin['backend'] == {'device': 'cpu', 'runtime_versions': NATIVE['runtime_versions']}
        vectors = db.execute('SELECT space_id, certificate_id, backend FROM people.embedding').fetchall()
        assert {(v['space_id'], v['certificate_id']) for v in vectors} == {(live, origin['id']), (legacy, None)}
        # The superseded link survived, and photos point at the main photo list.
        assert db.execute('SELECT count(*) n FROM people.face_detections WHERE superseded_by IS NOT NULL').fetchone()['n'] == 1
        assert db.execute("SELECT confrelid::regclass::text t FROM pg_constraint WHERE conrelid='people.face_analyses'::regclass "
                          "AND contype='f' AND conkey=(SELECT array_agg(attnum) FROM pg_attribute WHERE "
                          "attrelid='people.face_analyses'::regclass AND attname='media_id')").fetchone()['t'] == 'media'
        assert not db.execute("SELECT to_regclass('people.media') r").fetchone()['r']
        # New rows continue after the copied IDs.
        top = db.execute('SELECT max(id) m FROM people.people').fetchone()['m']
        assert db.execute("INSERT INTO people.people(name) VALUES('New') RETURNING id").fetchone()['id'] == top + 1
        db.rollback()


def test_face_uuids_are_the_same_in_every_copy_and_tell_twins_apart(cluster):
    _, library = cluster
    first, second = (run_migration(library()[1]) for _ in range(2))
    assert first['catalog_uuid'] == second['catalog_uuid']
    uuids = []
    for _ in range(2):
        _, connect = library()
        run_migration(connect)
        with connect() as db:
            uuids.append([(r['id'], str(r['face_uuid'])) for r in db.execute(
                'SELECT id, face_uuid FROM people.face_detections ORDER BY id')])
    assert uuids[0] == uuids[1]
    assert len({u for _, u in uuids[0]}) == 4
    # Photo 3's two faces share photo and box; the second gets the next ordinal.
    box = {'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.4}
    assert uuids[0][2][1] == str(catalog.face_uuid(f'{3:064x}', box))
    assert uuids[0][3][1] == str(catalog.face_uuid(f'{3:064x}', box, 1))


def test_the_new_catalog_cleans_up_after_a_face_in_its_own_tables(cluster):
    _, library = cluster
    _, connect = library()
    run_migration(connect)
    with connect('public') as db:
        did = db.execute('SELECT min(id) m FROM people.face_detections').fetchone()['m']
        db.execute('DELETE FROM people.face_detections WHERE id=%s', (did,))
        db.commit()
        assert db.execute('SELECT revision FROM people.people_revision').fetchone()['revision'] == 8
        assert db.execute('SELECT count(*) n FROM people.people_previews').fetchone()['n'] == 0
        assert db.execute('SELECT revision FROM public.people_revision').fetchone()['revision'] == 0


def test_a_second_run_and_a_failure_partway_change_nothing(cluster, monkeypatch):
    _, library = cluster
    _, connect = library()
    original = catalog._same_rows

    def fail_on_members(db, src, table, *rest):
        if table == 'auto_people_members':
            raise catalog.MigrationRefused('simulated difference')
        return original(db, src, table, *rest)
    monkeypatch.setattr(catalog, '_same_rows', fail_on_members)
    with connect() as db:
        with pytest.raises(catalog.MigrationRefused, match='simulated'):
            catalog.migrate(db, SOURCE)
        db.rollback()
        assert not db.execute("SELECT 1 FROM pg_namespace WHERE nspname='people'").fetchone()
    monkeypatch.setattr(catalog, '_same_rows', original)
    run_migration(connect)
    with connect() as db, pytest.raises(catalog.MigrationRefused, match='runs once'):
        catalog.migrate(db, SOURCE)


def test_manifests_policy_and_evidence_are_checked_against_the_source(cluster):
    _, library = cluster
    _, connect = library()
    run_migration(connect)
    s = sql.Identifier(SOURCE)
    with connect() as db:
        policy = db.execute('SELECT * FROM people.policy').fetchone()
        source = db.execute(sql.SQL('SELECT * FROM {}.native_matching_policies').format(s)).fetchone()
        assert (policy['policy'], policy['evidence'], policy['activated_at']) == (
            source['policy'], source['evidence'], source['activated_at'])
        assert {r['legacy_model_id']: r['manifest'] for r in db.execute('SELECT * FROM people.space')} == {
            'native-model': NATIVE, 'legacy-model': LEGACY}
        # The check that guards these copies sees a changed value.
        source_rows = sql.SQL('SELECT s.policy, s.evidence FROM {}.native_matching_policies s').format(s)
        target_rows = sql.SQL('SELECT policy, evidence FROM people.policy')
        with pytest.raises(catalog.MigrationRefused, match='differs'):
            catalog._insert_and_check(db, 'policy', sql.SQL("UPDATE people.policy SET evidence='{}'::jsonb"),
                                      source_rows, target_rows)
        db.rollback()


def test_a_refusal_from_the_command_says_why_without_a_traceback(cluster, tmp_path, capsys):
    from face_detect.cli import catalog as cli
    _, library = cluster
    _, connect = library()
    run_migration(connect)
    assert cli.main(['migrate', '--from', SOURCE], connect=connect, data_root=tmp_path) == 2
    err = capsys.readouterr().err
    assert err.startswith('Refused: ') and 'runs once' in err and 'Traceback' not in err


@pytest.mark.parametrize('problem', ['live-model-has-no-space', 'photo-not-in-photo-list', 'preferences-in-source',
                                     'manifest-not-an-object'])
def test_a_source_the_catalog_cannot_hold_is_refused_before_any_change(cluster, problem):
    _, library = cluster
    _, connect = library()
    s = sql.Identifier(SOURCE)
    with connect() as db:
        if problem == 'live-model-has-no-space':
            db.execute(sql.SQL("UPDATE {}.native_catalog_state SET model_id='legacy-model'").format(s))
        elif problem == 'photo-not-in-photo-list':
            db.execute(sql.SQL("INSERT INTO {}.media(id,sha256,relative_path,name,kind,size) OVERRIDING SYSTEM VALUE "
                               "VALUES(9,%s,'p9','p9','image',1)").format(s), ('9' * 64,))
            db.execute(sql.SQL("INSERT INTO {}.face_review_events(media_id,actor,action) VALUES(9,'f','seen')").format(s))
        elif problem == 'preferences-in-source':
            db.execute(sql.SQL("INSERT INTO {}.face_preferences(media_id,actor) VALUES(1,'f')").format(s))
        else:
            db.execute(sql.SQL("UPDATE {}.people_models SET manifest='[]' WHERE id='legacy-model'").format(s))
        db.commit()
        with pytest.raises(catalog.MigrationRefused):
            catalog.migrate(db, SOURCE)
        db.rollback()
        assert not db.execute("SELECT 1 FROM pg_namespace WHERE nspname='people'").fetchone()


def test_a_backup_of_the_new_catalog_is_classified_captured_and_restored(cluster, tmp_path):
    """The backup gate's database half: capture, verify and restore a migrated library."""
    from app.backup.compatibility import verify_restored_evidence
    from app.backup.inventory import inspect_snapshot
    from app.backup.postgres import Endpoint, capture_database
    from app.backup.restore_database import restore_database
    from test_backup_postgres import ample
    params, library = cluster
    name, connect = library()
    run_migration(connect)
    binary = Path(os.environ.get('FAMILYFOTO_BACKUP_PG_BIN', str(ROOT.parent / 'FamilyFoto/runtime/postgres/bin')))
    suffix = '.exe' if os.name == 'nt' else ''
    key = backup_key()
    artifact = tmp_path / 'catalog'
    manifest = capture_database(Endpoint(params['host'], params['port'], name, params['user']),
                                binary / ('pg_dump' + suffix), artifact, key, estimated_bytes=1024 ** 2,
                                guard_factory=ample)
    evidence = manifest['metadata']['compatibility']
    assert {(m['schema'], m['table']) for m in evidence['models']} >= {
        ('people', 'space'), ('people', 'detector'), ('people', 'policy'), ('people', 'catalog_state')}
    report = restore_database(artifact, key, Endpoint(params['host'], params['port'], 'postgres', params['user']),
                              binary / ('pg_restore' + suffix), expected_cluster_directory=ROOT / 'temp/backup-plan-pg',
                              required_bytes=1024 ** 2, guard_factory=ample)
    try:
        with psycopg.connect(**params, dbname=report['database']) as db:
            verify_restored_evidence(db, list(inspect_snapshot(db)), evidence)
            assert db.execute('SELECT count(*) FROM people.face_detections').fetchone()[0] == 4
    finally:
        with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(report['database'])))


def test_backup_rules_cover_the_new_catalog_and_nothing_else():
    for table in ('space', 'detector', 'embedding', 'certificate', 'policy', 'catalog_state', 'face_detections'):
        assert classify_table('people', table).category == 'authoritative'
    assert classify_table('people', 'people_previews').restore.startswith('preserve in encrypted dump')
    assert classify_table('people', 'native_migration_faces').category == 'authoritative'
    for table in ('media', 'jobs', 'unknown'):
        with pytest.raises(UnclassifiedState):
            classify_table('people', table)
    assert classify_file('data', 'people/thumbnails/12.jpg').category == 'rebuildable'
    check_directory('data', 'people')
    check_directory('data', 'people/thumbnails')
    with pytest.raises(UnclassifiedState):
        classify_file('data', 'people/other.json')


def test_thumbnails_are_copied_whole_or_not_at_all(tmp_path):
    origin = tmp_path / 'face_detect/profiles' / TOKEN / 'thumbnails'
    origin.mkdir(parents=True)
    for did in (1, 2, 30):
        (origin / f'{did}.jpg').write_bytes(f'face {did}'.encode())
    (origin / 'notes.txt').write_text('not a face')
    # A copy that stopped earlier is replaced, not trusted.
    (tmp_path / 'people/thumbnails.partial').mkdir(parents=True)
    (tmp_path / 'people/thumbnails.partial/1.jpg').write_bytes(b'stale')
    assert catalog.copy_thumbnails(tmp_path, SOURCE) == {'thumbnails': 3}
    target = tmp_path / 'people/thumbnails'
    assert sorted(p.name for p in target.iterdir()) == ['1.jpg', '2.jpg', '30.jpg']
    assert (target / '1.jpg').read_bytes() == b'face 1' and not (tmp_path / 'people/thumbnails.partial').exists()
    assert (origin / '1.jpg').exists(), 'The source folder is left as it was'
    with pytest.raises(catalog.MigrationRefused, match='already exists'):
        catalog.copy_thumbnails(tmp_path, SOURCE)


def test_the_command_migrates_then_copies_thumbnails(cluster, tmp_path, capsys):
    from face_detect.cli import catalog as cli
    _, library = cluster
    _, connect = library()
    (tmp_path / 'face_detect/profiles' / TOKEN / 'thumbnails').mkdir(parents=True)
    (tmp_path / 'face_detect/profiles' / TOKEN / 'thumbnails/2.jpg').write_bytes(b'face')
    assert cli.main(['migrate', '--from', SOURCE], connect=connect, data_root=tmp_path) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['thumbnails'] == 1 and out['tables']['face_detections'] == 4
    with pytest.raises(SystemExit):
        cli.main(['migrate', '--from', 'public'], connect=connect, data_root=tmp_path)


def test_collection_links_follow_the_catalog_and_the_profile_points_at_it(cluster, tmp_path, capsys):
    from face_detect.cli import catalog as cli
    _, library = cluster
    _, connect = library()
    with connect() as db:
        cid = db.execute("INSERT INTO album_collections(name) VALUES('Fixture') RETURNING id").fetchone()['id']
        db.execute('INSERT INTO album_collection_people VALUES(%s,%s,1,1)', (cid, TOKEN))
        db.execute("INSERT INTO album_collection_people VALUES(%s,%s,2,2)", (cid, 'f' * 32))
    profile = tmp_path / 'face_detect/profiles' / TOKEN
    profile.mkdir(parents=True)
    (profile / 'runtime.json').write_text(json.dumps({'schema': SOURCE, 'model_id': 'native-model', 'jobs': 'kept'}))
    assert cli.main(['migrate', '--from', SOURCE], connect=connect, data_root=tmp_path) == 0
    out = json.loads(capsys.readouterr().out)
    link = catalog.catalog_uuid(SOURCE).hex
    assert out['collection_links'] == {'pilots': [TOKEN, 'legacy-pilot'], 'rewritten': 1, 'link_pilot': link}
    with connect() as db:
        pilots = {r['person_id']: r['pilot'] for r in db.execute('SELECT * FROM album_collection_people')}
        from face_detect.catalog import link_pilot
        db.execute("SELECT set_config('search_path','people, public',false)")
        assert link_pilot(db) == link
    # This catalog's link moved to its UUID; another catalog's link was left as it was.
    assert pilots == {1: link, 2: 'f' * 32}
    runtime = json.loads((profile / 'runtime.json').read_text())
    assert runtime['schema'] == 'people' and runtime['previous_schema'] == SOURCE
    assert runtime['space_id'] == space_id(NATIVE) and runtime['jobs'] == 'kept'


class Provider:
    def __init__(self, manifest):
        from face_detect.spaces.identity import detector_id as detector
        self.manifest = manifest
        self.space_id, self.detector_id = space_id(manifest), detector(manifest)


def test_a_computer_scans_into_the_library_space_only_and_only_once_checked(cluster, monkeypatch):
    from face_detect.catalog.certify import NotCertified, ensure_certified, needs_check
    from face_detect.catalog.spaces import SpaceRefused, register_space, space_key, detector_key
    _, library = cluster
    _, connect = library()
    run_migration(connect)

    def people():
        return connect('people,public')
    # A Mac: same space, another device and runtime. It registers into the active space.
    mac = Provider(NATIVE | {'device': 'coreml', 'runtime_versions': {'onnxruntime': '1.30.0'}})
    with people() as db:
        assert register_space(db, mac) == space_id(NATIVE)
        # Older records name the space and detector by the old model ID.
        assert space_key(db, 'native-model') == space_id(NATIVE) and detector_key(db, 'native-model') == detector_id(NATIVE)
        assert needs_check(db, mac) is not None, 'Stored vectors need a check before this computer writes'
    # Another model is refused, and so is the retired October one.
    other = Provider(NATIVE | {'assets': {'recognizer': {'sha256': 'c' * 64}, 'detector': {'sha256': 'b' * 64}}})
    with people() as db, pytest.raises(SpaceRefused, match='Another face model'):
        register_space(db, other)
    # A failed check is recorded and stops the scan; a passed one lets it go on, and isn't repeated.
    calls = []

    def check(passed):
        def run():
            calls.append(passed)
            from face_detect.spaces.identity import backend, backend_id
            value = backend(mac.manifest)
            return {'space_id': mac.space_id, 'backend': value, 'backend_id': backend_id(value),
                    'reference': 'library-sample', 'passed': passed, 'reason': 'conforms' if passed else 'vectors-differ'}
        return run
    with pytest.raises(NotCertified, match='vectors-differ'):
        ensure_certified(people, mac, check(False))
    # A recent failure isn't rerun by every worker start; it's reported again.
    with pytest.raises(NotCertified, match='vectors-differ'):
        ensure_certified(people, mac, check(True))
    assert calls == [False]
    from face_detect.catalog import certify
    monkeypatch.setattr(certify, 'RETRY_SECONDS', 0)
    assert ensure_certified(people, mac, check(True))['passed'] is True
    assert ensure_certified(people, mac, check(True)) is None and calls == [False, True]
    with people() as db:
        refs = [r['reference'] for r in db.execute('SELECT reference FROM certificate ORDER BY id')]
        assert refs == ['origin', 'library-sample', 'library-sample']


def test_an_empty_library_needs_no_check_and_its_first_model_becomes_active(cluster):
    from face_detect.catalog import create
    from face_detect.catalog.certify import needs_check
    from face_detect.catalog.spaces import active_space, register_space
    _, library = cluster
    _, connect = library()
    with connect() as db:
        create(db)
    with connect('people,public') as db:
        provider = Provider(NATIVE)
        assert active_space(db) is None and needs_check(db, provider) is None
        assert register_space(db, provider) == space_id(NATIVE) and active_space(db) == space_id(NATIVE)
