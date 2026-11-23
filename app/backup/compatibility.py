"""Read-only database compatibility evidence captured inside the export snapshot."""
import hashlib
import json
import re

from psycopg import sql

from ..release import metadata as release_metadata, schema_compatibility


FORMAT = 'familyfoto-database-compatibility-v1'
ENVIRONMENT_KEYS = ('encoding', 'datcollate', 'datctype', 'datlocprovider',
                    'datlocale', 'daticulocale', 'daticurules', 'datcollversion')
MODEL_COLUMNS = {
    'people_models': ('id', 'manifest'),
    'face_models': ('id', 'manifest'),
    'native_matching_policies': ('model_id', 'policy', 'evidence'),
    'native_catalog_state': ('singleton', 'model_id', 'legacy_pilot'),
    # The one People catalog: what its vectors mean, where its boxes came from, how it
    # matches, and which space is live.
    'space': ('space_id', 'manifest', 'state', 'legacy_model_id'),
    'detector': ('detector_id', 'manifest', 'legacy_model_id'),
    'policy': ('policy_id', 'space_id', 'policy', 'evidence'),
    'catalog_state': ('singleton', 'active_space_id', 'catalog_uuid', 'migrated_from', 'legacy_pilot'),
}


def model_evidence(db, tables):
    """Fingerprint model spaces and qualified policies without loading inference."""
    result = []
    for table in sorted(tables, key=lambda item: (item['schema'], item['name'])):
        columns = MODEL_COLUMNS.get(table['name'])
        if columns is None:
            continue
        digest = hashlib.sha256()
        count = 0
        with db.cursor(name='backup_model_evidence') as rows:
            rows.itersize = 100
            # Text IDs use a locale-independent order; singleton is boolean.
            ordering = sql.Identifier(columns[0])
            if columns[0] != 'singleton':
                ordering += sql.SQL(' COLLATE "C"')
            rows.execute(sql.SQL('SELECT {} FROM {}.{} ORDER BY {}').format(
                sql.SQL(',').join(map(sql.Identifier, columns)),
                sql.Identifier(table['schema']), sql.Identifier(table['name']), ordering))
            for row in rows:
                # default=str only reaches values JSON can't hold (the catalog's UUID), so
                # fingerprints of the older tables are unchanged.
                digest.update(json.dumps(list(row), sort_keys=True, ensure_ascii=True, default=str,
                                         allow_nan=False, separators=(',', ':')).encode() + b'\n')
                count += 1
        result.append({'schema': table['schema'], 'table': table['name'],
                       'count': count, 'sha256': digest.hexdigest()})
    return result


def database_environment(db, name=None):
    row = db.execute('SELECT to_jsonb(d) FROM pg_database d WHERE datname=COALESCE(%s,current_database())',
                     (name,)).fetchone()
    if row is None:
        raise ValueError('Database environment is unavailable')
    raw = row[0]
    return {key: raw.get(key) for key in ENVIRONMENT_KEYS}


def database_evidence(db, tables):
    schemas = sorted({item['schema'] for item in tables})
    histories = {}
    for schema in schemas:
        if any(item['schema'] == schema and item['name'] == 'schema_versions' for item in tables):
            versions = [row[0] for row in db.execute(sql.SQL('SELECT version FROM {}.schema_versions ORDER BY version')
                                                    .format(sql.Identifier(schema)))]
            histories[schema] = versions
    digest = hashlib.sha256()
    count = 0
    with db.cursor(name='backup_function_evidence') as rows:
        rows.itersize = 100
        rows.execute('SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),'
                     'pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace '
                     "WHERE n.nspname=ANY(%s) AND p.prokind IN ('f','p') "
                     'ORDER BY n.nspname COLLATE "C",p.proname COLLATE "C",'
                     'pg_get_function_identity_arguments(p.oid) COLLATE "C"', (schemas,))
        for row in rows:
            digest.update(json.dumps(list(row), ensure_ascii=True, separators=(',', ':')).encode() + b'\n')
            count += 1
    extensions = [list(row) for row in db.execute('SELECT e.extname,e.extversion,n.nspname '
                 'FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace ORDER BY e.extname COLLATE "C"')]
    return {'format': FORMAT, 'environment': database_environment(db), 'schema_histories': histories,
            'extensions': extensions, 'functions': {'count': count, 'sha256': digest.hexdigest()},
            'models': model_evidence(db, tables)}


def validate_source_evidence(evidence):
    if not isinstance(evidence, dict) or evidence.get('format') != FORMAT:
        raise ValueError('Database backup lacks supported compatibility evidence')
    environment = evidence.get('environment')
    if (not isinstance(environment, dict) or set(environment) != set(ENVIRONMENT_KEYS)
            or type(environment.get('encoding')) is not int
            or any(value is not None and not isinstance(value, str)
                   for key, value in environment.items() if key != 'encoding')):
        raise ValueError('Invalid source database environment')
    extensions = evidence.get('extensions')
    if (not isinstance(extensions, list) or any(not isinstance(row, list) or len(row) != 3
            or any(not isinstance(value, str) or not value for value in row) for row in extensions)):
        raise ValueError('Invalid source extension evidence')
    functions = evidence.get('functions')
    if (not isinstance(functions, dict) or type(functions.get('count')) is not int or functions['count'] < 0
            or not isinstance(functions.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', functions['sha256'])):
        raise ValueError('Invalid source function evidence')
    histories = evidence.get('schema_histories')
    if not isinstance(histories, dict):
        raise ValueError('Invalid source schema histories')
    for versions in histories.values():
        # Do not substitute the target release's latest history for the source.
        if (not isinstance(versions, list) or any(type(v) is not int or v < 1 for v in versions)
                or any(version != index for index, version in enumerate(versions, 1))):
            raise ValueError('Source schema history is not contiguous')
    if 'public' in histories:
        status = schema_compatibility(histories['public'], release_metadata())
        if status in {'newer-database-refused', 'unsupported-upgrade'}:
            raise ValueError('Source schema is unsupported by this recovery release')
    models = evidence.get('models')
    if not isinstance(models, list):
        raise ValueError('Source model compatibility evidence is missing')
    identities = set()
    for model in models:
        if (not isinstance(model, dict) or not isinstance(model.get('schema'), str)
                or not isinstance(model.get('table'), str) or model['table'] not in MODEL_COLUMNS
                or type(model.get('count')) is not int
                or model['count'] < 0 or not isinstance(model.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', model['sha256'])):
            raise ValueError('Invalid model compatibility evidence')
        identity = (model['schema'], model['table'])
        if identity in identities:
            raise ValueError('Duplicate model compatibility evidence')
        identities.add(identity)


def preflight_target(db, evidence):
    validate_source_evidence(evidence)
    if database_environment(db, 'template0') != evidence['environment']:
        raise ValueError('Database locale or encoding differs; cross-locale recovery requires qualification')
    available = dict(db.execute('SELECT name,default_version FROM pg_available_extensions'))
    for name, version, _schema in evidence['extensions']:
        if available.get(name) != version:
            raise ValueError('Required extension default version is unavailable on recovery cluster')


def verify_restored_evidence(db, tables, expected):
    if database_evidence(db, tables) != expected:
        raise ValueError('Restored schema, model, function, extension or locale evidence differs from backup')
