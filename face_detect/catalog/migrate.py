"""Move a per-model People catalog (`face_native_<token>`) into the one catalog, `people`.

See docs/PEOPLE.md. It runs no model: it copies rows and renames keys, in
one transaction, and checks every copied table against its source before committing.
Nothing in the source schema changes, so rolling back is pointing the app at it again.

- **IDs are kept.** Face, person, group and event numbers stay what they were, so decisions,
  the undo history and thumbnail names stay valid.
- **Model identities become spaces and detectors.** A model whose manifest gives a space ID
  maps to it. One that doesn't (the October CompreFace model) becomes a retired legacy space,
  which is never used for matching. The live model must give a space.
- **Faces get a lasting UUID**, derived from the photo's content hash and the stored box, so
  migrating two copies of the same library gives the same IDs. Faces sharing both are told
  apart by their order of detection ID, so a library whose face IDs were renumbered would
  give those few faces different UUIDs. Copies of one library keep their IDs, so they agree.
- **Photos are public.media.** The source's shadow copy of the photo list isn't carried over.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from psycopg import sql

from face_detect.contracts import ConfigurationError
from face_detect.spaces.identity import backend_id, detector_id, recorded_backend, space_id

from face_detect.catalog import SCHEMA, create as create_catalog

SOURCE = re.compile(r'face_native_[0-9a-f]{32}')
# Fixed forever: changing it would give every face a new UUID.
FACE_NAMESPACE = uuid.UUID('5d0a3c1e-6f2b-5b8e-9a41-2f7c8e1d4b60')
CATALOG_NAMESPACE = uuid.UUID('9b2e7f40-1c3d-5e6a-8f90-4a1b2c3d4e5f')


class MigrationRefused(ValueError):
    pass


def face_uuid(content_hash, box, ordinal=0):
    """A face's UUID from its photo and box. `ordinal` tells apart faces with the same of both."""
    try:
        name = ':'.join([content_hash] + [f"{float(box[k]):.6f}" for k in ('x', 'y', 'width', 'height')])
    except (KeyError, TypeError, ValueError):
        name = content_hash + ':' + json.dumps(box, sort_keys=True, separators=(',', ':'))
    return uuid.uuid5(FACE_NAMESPACE, name + (f':{ordinal}' if ordinal else ''))


def catalog_uuid(source):
    return uuid.uuid5(CATALOG_NAMESPACE, source)


def _id(manifest, compute, legacy):
    try:
        return compute(manifest), False
    except (ConfigurationError, KeyError, TypeError, ValueError):
        return 'legacy-' + legacy, True


# (target table, source table, columns copied as they are, {target column: source column})
# Tables whose key column is renamed list the source column; the value is mapped by a key map.
COPIED = (
    ('face_runs', 'face_runs', 'id actor state error heartbeat_at created_at updated_at active_seconds telemetry_epoch redetect', {'detector_id': 'model_id'}),
    ('face_analyses', 'face_analyses', 'id media_id content_hash width height outcome timings created_at revision staged run_id', {'detector_id': 'model_id'}),
    ('face_work', 'face_work', 'run_id media_id content_hash state generation attempts error updated_at', {}),
    ('face_review_events', 'face_review_events', 'id media_id actor action created_at', {}),
    ('people_runs', 'people_runs', 'id actor state error heartbeat_at active_seconds created_at updated_at', {'space_id': 'model_id'}),
    ('people', 'people', 'id name created_at deleted_at', {}),
    ('auto_people_groups', 'auto_people_groups', 'id person_id created_at', {}),
    ('people_suggestions', 'people_suggestions', 'id run_id detection_ids min_similarity', {'space_id': 'model_id'}),
    ('people_events', 'people_events', 'id action actor before_state after_revision undone created_at', {}),
    ('people_lifecycle_events', 'people_lifecycle_events', 'id person_id action actor old_name new_name created_at', {}),
    ('auto_people_events', 'auto_people_events', 'id action origin actor before_state after_revision undone created_at', {}),
    ('auto_people_receipts', 'auto_people_receipts', 'id input_hash result created_at', {'space_id': 'model_id'}),
    ('auto_people_scan_results', 'auto_people_scan_results', 'analysis_id items', {}),
    # face_detections is copied separately, with face_uuid.
    ('face_redetect_targets', 'face_redetect_targets', 'run_id detection_id', {}),
    ('face_ignore_events', 'face_ignore_events', 'id detection_id actor action created_at', {}),
    ('people_work', 'people_work', 'run_id detection_id media_id content_hash state generation attempts error updated_at', {}),
    ('people_assignments', 'people_assignments', 'detection_id person_id', {}),
    ('people_constraints', 'people_constraints', 'a b', {}),
    ('auto_people_face_quality', 'auto_people_face_quality', 'detection_id policy metrics state created_at released_at', {}),
    ('auto_people_members', 'auto_people_members', 'detection_id group_id origin', {'space_id': 'model_id'}),
    ('auto_people_observations', 'auto_people_observations', 'detection_id seed', {'space_id': 'model_id'}),
    ('auto_people_blocks', 'auto_people_blocks', 'detection_id person_id', {}),
    ('native_preserved_photos', 'native_preserved_photos', 'media_id content_hash', {}),
    ('native_migration_faces', 'native_migration_faces', 'detection_id state result created_at', {'space_id': 'model_id'}),
)
DETECTIONS = 'id analysis_id box landmarks landmark_schema score review_state ignored ignore_history accepted superseded_by'
# Tables in the source that must be empty, because the catalog doesn't carry them.
NOT_CARRIED_IF_EMPTY = ('face_preferences',)
# Rows whose photo must be in public.media, because the catalog's keys point there now.
BY_MEDIA = ('face_analyses', 'face_work', 'face_review_events', 'people_work', 'native_preserved_photos')


def _exists(db, schema, table):
    return db.execute('SELECT to_regclass(%s) IS NOT NULL present', (f'"{schema}"."{table}"',)).fetchone()['present']


def _key_maps(db, src):
    active = db.execute(sql.SQL('SELECT model_id FROM {}.native_catalog_state WHERE singleton').format(src)).fetchone()
    if not active:
        raise MigrationRefused('The source catalog has no live model (native_catalog_state)')
    spaces, detectors = {}, {}
    for row in db.execute(sql.SQL('SELECT id, manifest FROM {}.people_models ORDER BY id').format(src)).fetchall():
        if not isinstance(row['manifest'], dict):
            raise MigrationRefused('A model manifest is not an object; nothing was changed')
        value, legacy = _id(row['manifest'], space_id, row['id'])
        if row['id'] == active['model_id'] and legacy:
            raise MigrationRefused('The live model has no embedding space; nothing was changed')
        spaces[row['id']] = {'space_id': value, 'manifest': row['manifest'], 'legacy': legacy,
                             'state': 'active' if row['id'] == active['model_id'] else 'retired',
                             # Worked out here so a manifest that can't give one is refused
                             # before anything is created.
                             'backend': recorded_backend(row['manifest'])}
    for row in db.execute(sql.SQL('SELECT id, manifest FROM {}.face_models ORDER BY id').format(src)).fetchall():
        if not isinstance(row['manifest'], dict):
            raise MigrationRefused('A detector manifest is not an object; nothing was changed')
        value, _ = _id(row['manifest'], detector_id, row['id'])
        detectors[row['id']] = {'detector_id': value, 'manifest': row['manifest']}
    for name, mapped, key in (('space', spaces, 'space_id'), ('detector', detectors, 'detector_id')):
        values = [item[key] for item in mapped.values()]
        if len(values) != len(set(values)):
            raise MigrationRefused(f'Two old models map to the same {name}; that needs a decision first')
    return active['model_id'], spaces, detectors


def _preflight(db, source):
    src = sql.Identifier(source)
    if db.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (SCHEMA,)).fetchone():
        raise MigrationRefused('The people catalog already exists; the migration runs once')
    for table in NOT_CARRIED_IF_EMPTY:
        if _exists(db, source, table) and db.execute(
                sql.SQL('SELECT EXISTS(SELECT 1 FROM {}.{}) x').format(src, sql.Identifier(table))).fetchone()['x']:
            raise MigrationRefused(f'{table} in the source holds rows the catalog would not carry')
    missing = {}
    for table in BY_MEDIA:
        if not _exists(db, source, table):
            continue
        n = db.execute(sql.SQL('SELECT count(*) n FROM {}.{} t WHERE t.media_id IS NOT NULL AND NOT EXISTS '
                               '(SELECT 1 FROM public.media m WHERE m.id=t.media_id)').format(
            src, sql.Identifier(table))).fetchone()['n']
        if n:
            missing[table] = n
    if missing:
        raise MigrationRefused(f'Rows name photos that are not in the photo list: {missing}')


def _map_table(db, name, mapping, key):
    db.execute(sql.SQL('CREATE TEMP TABLE {}(old text PRIMARY KEY, new text NOT NULL) ON COMMIT DROP').format(
        sql.Identifier(name)))
    with db.cursor() as cursor, cursor.copy(sql.SQL('COPY {} (old, new) FROM STDIN').format(sql.Identifier(name))) as copy:
        for old, item in mapping.items():
            copy.write_row((old, item[key]))


def _select(table, src_table, plain, renamed):
    """The source rows as the target holds them, with renamed keys mapped."""
    columns = [sql.SQL('s.{}').format(sql.Identifier(c)) for c in plain.split()]
    joins = []
    for target, source in renamed.items():
        mapper = 'detector_map' if target == 'detector_id' else 'space_map'
        alias = sql.Identifier('m_' + target)
        columns.append(sql.SQL('{}.new AS {}').format(alias, sql.Identifier(target)))
        joins.append(sql.SQL('LEFT JOIN {} {} ON {}.old = s.{}').format(
            sql.Identifier(mapper), alias, alias, sql.Identifier(source)))
    names = plain.split() + list(renamed)
    return names, lambda src: sql.SQL('SELECT {} FROM {}.{} s {}').format(
        sql.SQL(', ').join(columns), src, sql.Identifier(src_table), sql.SQL(' ').join(joins))


def _identity(db, table):
    return db.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attidentity<>''",
                      (f'{SCHEMA}.{table}',)).fetchone()


def _copy(db, src, table, src_table, plain, renamed):
    names, select = _select(table, src_table, plain, renamed)
    overriding = sql.SQL(' OVERRIDING SYSTEM VALUE') if _identity(db, table) else sql.SQL('')
    db.execute(sql.SQL('INSERT INTO {}.{} ({}){} ').format(
        sql.Identifier(SCHEMA), sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, names)), overriding)
        + select(src))
    return names, select


def _same_rows(db, src, table, names, select, target=None):
    """Every source row is in the target and the other way round, duplicates counted."""
    if target is None:
        target = sql.SQL('SELECT {} FROM {}.{}').format(
            sql.SQL(', ').join(map(sql.Identifier, names)), sql.Identifier(SCHEMA), sql.Identifier(table))
    source = select(src)
    extra = db.execute(sql.SQL('SELECT count(*) n FROM (({}) EXCEPT ALL ({})) x').format(source, target)).fetchone()['n']
    more = db.execute(sql.SQL('SELECT count(*) n FROM (({}) EXCEPT ALL ({})) x').format(target, source)).fetchone()['n']
    if extra or more:
        raise MigrationRefused(f'{table} differs from its source after copying; nothing was changed')


def _insert_and_check(db, table, insert, source, target):
    """Copy rows with `insert`, then check `source` and `target` hold the same rows."""
    db.execute(insert)
    _same_rows(db, None, table, None, lambda _: source, target)


def _detections(db, src):
    rows = db.execute(sql.SQL('SELECT d.id, a.content_hash, d.box FROM {}.face_detections d '
                              'JOIN {}.face_analyses a ON a.id=d.analysis_id ORDER BY d.id').format(src, src)).fetchall()
    seen = {}
    db.execute('CREATE TEMP TABLE face_uuid_map(id bigint PRIMARY KEY, face_uuid uuid NOT NULL) ON COMMIT DROP')
    with db.cursor() as cursor, cursor.copy('COPY face_uuid_map (id, face_uuid) FROM STDIN') as copy:
        for row in rows:
            base = face_uuid(row['content_hash'], row['box'])
            ordinal = seen.get(base, 0)
            seen[base] = ordinal + 1
            copy.write_row((row['id'], face_uuid(row['content_hash'], row['box'], ordinal)))
    columns = DETECTIONS.split()
    # superseded_by may point forward, so it's filled once every face is in.
    plain = [c for c in columns if c != 'superseded_by']
    db.execute(sql.SQL('INSERT INTO {}.face_detections ({}, face_uuid) OVERRIDING SYSTEM VALUE '
                       'SELECT {}, u.face_uuid FROM {}.face_detections s JOIN face_uuid_map u ON u.id=s.id').format(
        sql.Identifier(SCHEMA), sql.SQL(', ').join(map(sql.Identifier, plain)),
        sql.SQL(', ').join(sql.SQL('s.{}').format(sql.Identifier(c)) for c in plain), src))
    db.execute(sql.SQL('UPDATE {}.face_detections t SET superseded_by=s.superseded_by FROM {}.face_detections s '
                       'WHERE s.id=t.id AND s.superseded_by IS NOT NULL').format(sql.Identifier(SCHEMA), src))
    names = columns
    def select(src):
        return sql.SQL('SELECT {} FROM {}.face_detections s').format(
            sql.SQL(', ').join(sql.SQL('s.{}').format(sql.Identifier(c)) for c in columns), src)
    _same_rows(db, src, 'face_detections', names, select)
    if len(rows) != db.execute('SELECT count(DISTINCT face_uuid) n FROM people.face_detections').fetchone()['n']:
        raise MigrationRefused('Face UUIDs are not unique')
    return len(rows)


def _sequences(db):
    for row in db.execute("SELECT c.relname tbl, a.attname col FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                          "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND a.attidentity<>''",
                          (SCHEMA,)).fetchall():
        table, column = sql.Identifier(row['tbl']), sql.Identifier(row['col'])
        db.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s, %s), m, true) FROM "
                           "(SELECT max({}) m FROM {}.{}) x WHERE m IS NOT NULL").format(column, sql.Identifier(SCHEMA), table),
                   (f'{SCHEMA}.{row["tbl"]}', row['col']))


def migrate(db, source, *, now=None):
    """Create `people` from `source` in the caller's transaction. Returns a report of counts.

    The caller commits. Anything wrong raises before that, and a rollback leaves no `people`.
    """
    if not SOURCE.fullmatch(source or ''):
        raise MigrationRefused('The source must be a face_native_<token> catalog')
    if not db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (source,)).fetchone():
        raise MigrationRefused('The source catalog does not exist')
    src = sql.Identifier(source)
    _preflight(db, source)
    active, spaces, detectors = _key_maps(db, src)
    create_catalog(db)
    _map_table(db, 'space_map', spaces, 'space_id')
    _map_table(db, 'detector_map', detectors, 'detector_id')
    db.execute('CREATE TEMP TABLE space_state(old text PRIMARY KEY, state text NOT NULL) ON COMMIT DROP')
    for old, item in spaces.items():
        db.execute('INSERT INTO space_state VALUES (%s, %s)', (old, item['state']))
    # Manifests are copied in the database, never through Python, and checked like the rest.
    _insert_and_check(
        db, 'space',
        sql.SQL('INSERT INTO people.space(space_id, manifest, state, legacy_model_id) '
                'SELECT m.new, s.manifest, t.state, s.id FROM {}.people_models s '
                'JOIN space_map m ON m.old=s.id JOIN space_state t ON t.old=s.id').format(src),
        sql.SQL('SELECT m.new, s.manifest, s.id FROM {}.people_models s JOIN space_map m ON m.old=s.id').format(src),
        sql.SQL('SELECT space_id, manifest, legacy_model_id FROM people.space'))
    _insert_and_check(
        db, 'detector',
        sql.SQL('INSERT INTO people.detector(detector_id, manifest, legacy_model_id) '
                'SELECT m.new, s.manifest, s.id FROM {}.face_models s JOIN detector_map m ON m.old=s.id').format(src),
        sql.SQL('SELECT m.new, s.manifest, s.id FROM {}.face_models s JOIN detector_map m ON m.old=s.id').format(src),
        sql.SQL('SELECT detector_id, manifest, legacy_model_id FROM people.detector'))

    report = {'source': source, 'spaces': {item['space_id']: item['state'] for item in spaces.values()}, 'tables': {}}
    detections_done = False
    for table, src_table, plain, renamed in COPIED:
        if table == 'face_redetect_targets' and not detections_done:
            report['tables']['face_detections'] = _detections(db, src)
            detections_done = True
        if not _exists(db, source, src_table):
            continue
        names, select = _copy(db, src, table, src_table, plain, renamed)
        _same_rows(db, src, table, names, select)
        report['tables'][table] = db.execute(sql.SQL('SELECT count(*) n FROM {}.{}').format(
            sql.Identifier(SCHEMA), sql.Identifier(table))).fetchone()['n']

    # Vectors: each records the machinery that made it. The live space's are tied to an
    # origin record of the backend in its manifest; retired spaces' to none.
    origin = spaces[active]['backend']
    certificate = db.execute(
        "INSERT INTO people.certificate(space_id, backend, backend_id, reference, passed, result) "
        "VALUES (%s, %s, %s, 'origin', false, %s) RETURNING id",
        (spaces[active]['space_id'], json.dumps(origin), backend_id(origin),
         json.dumps({'note': 'The backend that made the stored vectors, from their manifest. Not a check.'}))).fetchone()['id']
    db.execute('CREATE TEMP TABLE backend_map(old text PRIMARY KEY, backend jsonb, certificate_id bigint) ON COMMIT DROP')
    for old, item in spaces.items():
        db.execute('INSERT INTO backend_map VALUES (%s, %s, %s)',
                   (old, json.dumps(item['backend']), certificate if old == active else None))
    db.execute(sql.SQL('INSERT INTO people.embedding(detection_id, space_id, vector, backend, certificate_id, created_at) '
                       'SELECT s.detection_id, m.new, s.vector, b.backend, b.certificate_id, s.created_at '
                       'FROM {}.people_embeddings s JOIN space_map m ON m.old=s.model_id '
                       'JOIN backend_map b ON b.old=s.model_id').format(src))
    names, select = _select('embedding', 'people_embeddings', 'detection_id vector created_at', {'space_id': 'model_id'})
    _same_rows(db, src, 'embedding', names, select)
    report['tables']['embedding'] = db.execute('SELECT count(*) n FROM people.embedding').fetchone()['n']

    if _exists(db, source, 'native_matching_policies'):
        # The policy and its evidence are what the move must keep, so they're copied in the
        # database and checked like every other table. A NULL is refused by the target.
        _insert_and_check(
            db, 'policy',
            sql.SQL("INSERT INTO people.policy(policy_id, space_id, policy, evidence, activated_at) "
                    "SELECT encode(sha256(convert_to(m.new || ':' || s.policy::text, 'UTF8')), 'hex'), "
                    "m.new, s.policy, s.evidence, s.activated_at FROM {}.native_matching_policies s "
                    "JOIN space_map m ON m.old=s.model_id").format(src),
            sql.SQL('SELECT m.new, s.policy, s.evidence, s.activated_at FROM {}.native_matching_policies s '
                    'JOIN space_map m ON m.old=s.model_id').format(src),
            sql.SQL('SELECT space_id, policy, evidence, activated_at FROM people.policy'))
        report['tables']['policy'] = db.execute('SELECT count(*) n FROM people.policy').fetchone()['n']

    revision = db.execute(sql.SQL('SELECT revision FROM {}.people_revision').format(src)).fetchone()
    db.execute('UPDATE people.people_revision SET revision=%s', (revision['revision'] if revision else 0,))
    if revision and db.execute('SELECT revision FROM people.people_revision').fetchone()['revision'] != revision['revision']:
        raise MigrationRefused('people_revision differs from its source after copying; nothing was changed')
    legacy = db.execute(sql.SQL('SELECT legacy_pilot FROM {}.native_catalog_state').format(src)).fetchone()['legacy_pilot']
    db.execute('INSERT INTO people.catalog_state(active_space_id, catalog_uuid, migrated_from, legacy_pilot, created_at) '
               'VALUES (%s, %s, %s, %s, %s)',
               (spaces[active]['space_id'], catalog_uuid(source), source, legacy, now or datetime.now(timezone.utc)))
    _sequences(db)
    # Collection links name the catalog by its pilot, which differs per computer. They now name
    # it by the catalog's UUID. The old values are in the report, for a rollback.
    if db.execute("SELECT to_regclass('public.album_collection_people') r").fetchone()['r']:
        old = [source.removeprefix('face_native_')] + ([legacy] if legacy else [])
        report['collection_links'] = {'pilots': old, 'rewritten': db.execute(
            'UPDATE public.album_collection_people SET pilot=%s WHERE pilot=ANY(%s)',
            (catalog_uuid(source).hex, old)).rowcount, 'link_pilot': catalog_uuid(source).hex}
    report['catalog_uuid'] = str(catalog_uuid(source))
    report['active_space_id'] = spaces[active]['space_id']
    return report


def copy_thumbnails(data_root, source):
    """Copy the source profile's face thumbnails to data/people/thumbnails, checking each one.

    The folder is written under a temporary name and renamed when complete, so a folder at
    the final name is always whole. A missing thumbnail isn't an error: they're a cache.
    """
    if not SOURCE.fullmatch(source or ''):
        raise MigrationRefused('The source must be a face_native_<token> catalog')
    origin = Path(data_root) / 'face_detect/profiles' / source.removeprefix('face_native_') / 'thumbnails'
    target = Path(data_root) / 'people/thumbnails'
    if target.exists():
        raise MigrationRefused('data/people/thumbnails already exists')
    partial = target.with_name('thumbnails.partial')
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    copied = 0
    if origin.is_dir():
        for path in sorted(origin.glob('*.jpg')):
            if not re.fullmatch(r'[1-9][0-9]*\.jpg', path.name):
                continue
            data = path.read_bytes()
            out = partial / path.name
            with open(out, 'xb') as handle:
                handle.write(data)
            if hashlib.sha256(out.read_bytes()).digest() != hashlib.sha256(data).digest():
                raise MigrationRefused(f'Thumbnail {path.name} did not copy intact')
            copied += 1
    partial.rename(target)
    return {'thumbnails': copied}
