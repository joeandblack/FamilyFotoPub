"""Explicit metadata-only deployment stage; no scanning or media-file mutations."""
import re

PEOPLE_WRITES = {
    '/groups/{gid}/name', '/groups/{gid}/cover', '/groups/{gid}/selected-faces',
    '/groups/{gid}/order', '/groups/{gid}/position', '/people/{pid}/rename',
    '/faces/{did}/correct', '/faces/{did}/ignored', '/events/{eid}/undo',
    '/review-actions/stream', '/review/set-aside', '/review/match', '/review/ignore',
    '/other/match', '/other/ignore', '/groups/{gid}/match',
}
PUBLIC_TABLES = {'albums', 'album_items', 'album_collections',
                 'album_collection_members', 'album_collection_people'}
PEOPLE_TABLES = {'people', 'people_assignments', 'people_revision', 'people_previews',
                 'auto_people_groups', 'auto_people_members', 'auto_people_events',
                 'auto_people_blocks', 'face_ignore_events'}


def allowed_write(method, path):
    if method == 'POST' and path.startswith('/api/people-preview'):
        suffix = path.removeprefix('/api/people-preview')
        return any(re.fullmatch(re.sub(r'\{\w+\}', '[1-9][0-9]*', route), suffix)
                   for route in PEOPLE_WRITES)
    patterns = {
        'POST': [r'/api/albums', r'/api/albums/membership',
                 r'/api/albums/[1-9][0-9]*/(items|remove|transfer|position)',
                 r'/api/albums/[1-9][0-9]*/items/[1-9][0-9]*/(cover|move|position)',
                 r'/api/album-collections', r'/api/album-collections/order/root',
                 r'/api/album-collections/[1-9][0-9]*/(parent|cover|albums|remove|people|remove-people|order)'],
        'PATCH': [r'/api/(albums|album-collections)/[1-9][0-9]*(/cover-framing)?'],
        'DELETE': [r'/api/(albums|album-collections)/[1-9][0-9]*'],
    }
    return any(re.fullmatch(pattern, path) for pattern in patterns.get(method, []))


def verify_database(cfg):
    import psycopg
    from face_detect.api.readonly_people import catalog_settings
    schema = catalog_settings(cfg['preview']['data_root'])['schema']
    with psycopg.connect(**cfg['database']) as db:
        role = db.execute('SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls '
                          'FROM pg_roles WHERE rolname=current_user').fetchone()
        if any(role):
            raise RuntimeError('Limited-edit role must be unprivileged')
        if db.execute('SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles '
                      'WHERE rolname=current_user)').fetchone():
            raise RuntimeError('Limited-edit role must not inherit other roles')
        for scope, tables in [('public', PUBLIC_TABLES), (schema, PEOPLE_TABLES)]:
            if db.execute("SELECT has_schema_privilege(current_user,%s,'CREATE')", (scope,)).fetchone()[0]:
                raise RuntimeError('Limited-edit role cannot create schema objects')
            rows = db.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s AND "
                              "has_table_privilege(current_user,quote_ident(schemaname)||'.'||quote_ident(tablename),"
                              "'INSERT,UPDATE,DELETE,TRUNCATE')", (scope,)).fetchall()
            if {row[0] for row in rows} - tables:
                raise RuntimeError('Unexpected table write grant')
            if db.execute("SELECT 1 FROM pg_tables WHERE schemaname=%s AND "
                          "has_table_privilege(current_user,quote_ident(schemaname)||'.'||quote_ident(tablename),'TRUNCATE') LIMIT 1",
                          (scope,)).fetchone():
                raise RuntimeError('Limited-edit role cannot truncate tables')
            # Row locks require UPDATE privilege on at least one column. Only
            # the display name is granted, never identity, path, hash or content.
            for table, permitted in [('media', {'name'}), ('face_detections', {'ignored', 'ignore_history'} if scope == schema else set())]:
                columns = db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                                     "AND table_name=%s AND has_column_privilege(current_user,"
                                     "quote_ident(table_schema)||'.'||quote_ident(table_name),column_name,'UPDATE')",
                                     (scope, table)).fetchall()
                if {row[0] for row in columns} - permitted:
                    raise RuntimeError('Unexpected media/detection column write grant')
        return db.execute('SELECT count(*) FROM public.media').fetchone()[0]
