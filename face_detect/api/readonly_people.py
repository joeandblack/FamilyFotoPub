"""Opt-in viewing of a legacy catalog without starting its worker or migrating it."""
import copy
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

READ_ROUTES = {
    '', '/named-people', '/cards', '/review-page', '/review-imports',
    '/auto-organize', '/suggestions', '/ignored', '/blurry', '/groups/{gid}',
}


def thumbnail_query(kind):
    from face_detect.recognition import ignored_faces, automatic_people
    if kind == 'blurry':
        from face_detect.recognition.people_face_quality import visible_sql
        where = visible_sql()
    elif kind == 'ignored':
        where = ignored_faces.BASE + ' AND d.ignored'
    elif kind == 'group':
        where = automatic_people.ELIGIBLE
    else:
        raise ValueError('Unknown face thumbnail collection')
    if kind != 'blurry':
        where += ' AND EXISTS(SELECT 1 FROM auto_people_members gm WHERE gm.detection_id=d.id)'
    return 'SELECT d.id ' + where + ' AND d.id=%s'


def catalog_settings(data_root, *, recovery=False):
    data_root = Path(data_root).resolve()
    settings = json.loads((data_root / 'people-preview.json').read_text())
    pilot = settings.get('pilot', '')
    if settings.get('enabled') is not True or not re.fullmatch('[0-9a-f]{32}', pilot):
        raise ValueError('An existing People catalog is required')
    native = settings.get('backend') == 'native'
    if native:
        root = (data_root / 'face_detect/profiles' / pilot).resolve()
        if not root.is_relative_to(data_root):
            raise ValueError('People catalog must remain inside the data directory')
        runtime = json.loads((root/'runtime.json').read_text())
        from face_detect import catalog
        if runtime.get('schema') == 'face_native_'+pilot:
            # A library from before the one catalog. It is migrated when it is activated; its
            # People data can't be shown until then.
            raise ValueError('This People catalog is from before the one catalog; run the catalog migration to see its People')
        # The one catalog is checked when it's made (the migration compares every table with its
        # source), so it needs no per-computer 'preserved' mark, unlike the older per-model ones.
        if runtime.get('schema') != catalog.SCHEMA:
            raise ValueError('Verified preserved native catalog required')
        return {'pilot': pilot, 'root': root, 'schema': catalog.SCHEMA,
                'thumbnails': (data_root / 'people/thumbnails').resolve(), 'data_root': data_root,
                'native': True, 'legacy_pilot': runtime.get('legacy_pilot'),
                'previous_pilots': runtime.get('previous_pilots', [])}
    # Persisted historical data location, not an installed inference provider.
    # Renaming it would orphan old thumbnails and migration source catalogs.
    root = (data_root / 'compreface/raw-local' / pilot).resolve()
    if not root.is_relative_to(data_root):
        raise ValueError('People catalog must remain inside the data directory')
    runtime = json.loads((root / 'runtime.json').read_text())
    schema = runtime.get('schema')
    if schema != 'auto_eval_' + pilot:
        raise ValueError('People catalog schema does not match its identity')
    return {'pilot': pilot, 'root': root, 'schema': schema}



# The legacy catalog is a snapshot. Always apply current library exclusions.
VISIBILITY = ''' AND EXISTS(SELECT 1 FROM public.media live
    WHERE live.id=m.id AND live.sha256=m.sha256 AND live.kind='image'
    AND left(live.name,1)<>'.'
    AND public.familyfoto_can_see(live.owner_name,live.visibility)
    AND NOT EXISTS(SELECT 1 FROM public.media_trash t WHERE t.media_id=live.id)
    AND NOT EXISTS(SELECT 1 FROM public.face_preferences p
                   WHERE p.media_id=live.id AND p.opted_out))'''


def catalog_database(cfg, settings):
    from face_detect import catalog
    if settings['schema'] == catalog.SCHEMA:
        return catalog.database(cfg)
    database = copy.deepcopy(cfg['database'])
    database['options'] = (database.get('options', '') +
                           ' -c search_path=' + settings['schema'])
    return database


def apply_live_visibility():
    from face_detect.recognition import automatic_people as service
    from face_detect.recognition import ignored_faces, people_review_scope
    from face_detect.recognition.people_face_quality import NOT_BLURRY

    people_review_scope.LIVE_VISIBILITY = VISIBILITY
    if VISIBILITY not in ignored_faces.BASE:
        ignored_faces.BASE += VISIBILITY
    service.ELIGIBLE = (ignored_faces.BASE +
                       " AND m.kind='image' AND NOT d.ignored AND d.review_state<>'rejected'" + NOT_BLURRY)
    service.load_config = lambda: {'automatic_people': {'enabled': True}}


def bind_catalog(cfg, settings, connect):
    from face_detect.api import automatic_people_api as api
    from face_detect.api import people_preview_gateway as gateway

    api.connection = connect
    if not cfg.get('live_existing_catalog'):
        gateway.settings = lambda: settings


def drop_network_bridge(app):
    """Replace the network bridge with explicitly allowlisted in-process reads."""
    app.router.routes[:] = [route for route in app.router.routes
                           if not getattr(route, 'path', '').startswith(('/api/people-preview', '/people-preview'))]


def add_native_capabilities(router, connect):
    from app.auth.deps import reader

    @router.get('/capabilities')
    def native_capabilities(user=Depends(reader)):
        from face_detect.recognition.policy import manual_policy, manual_enabled
        with connect() as db:
            from face_detect.catalog.spaces import active_space
            policy=manual_policy(db,active_space(db))
        return {'native':True,'automatic_matching':manual_enabled(policy),
                'manual_matching_trial':policy.get('manual_enabled') is True}


def add_allowlisted_routes(router, cfg):
    from app.jobs.limited_edit import PEOPLE_WRITES
    from face_detect.api import automatic_people_api as api

    for route in api.router.routes:
        suffix = route.path.removeprefix('/api/automatic-people')
        if suffix in READ_ROUTES and route.methods == {'GET'}:
            router.add_api_route(suffix, route.endpoint, methods=['GET'])
        if route.methods == {'POST'} and (cfg.get('live_existing_catalog') is True or
                (cfg.get('limited_metadata_edit') is True and suffix in PEOPLE_WRITES)):
            router.add_api_route(suffix, route.endpoint, methods=['POST'])


def import_progress(settings):
    result={}
    for path in (settings['root']/'jobs').glob('import-*/import.json'):
        if not re.fullmatch(r'import-[1-9][0-9]*',path.parent.name):continue
        value=json.loads(path.read_text())
        control=path.parent/'control.json'
        action=json.loads(control.read_text()).get('action') if control.exists() else 'pause'
        state=value.get('state','paused')
        if state not in ('completed','cancelled') and action!='run':
            state='paused' if action=='pause' else 'cancelled'
        items=value.get('items',[])
        result[path.parent.name.removeprefix('import-')]={
            'state':state,'done':sum(i.get('state')=='done' for i in items),
            'total':len(items),'excluded':sum(i.get('state')=='excluded' for i in items),
            'run_seconds':value.get('run_seconds'),'errors':[
                {'media_id':i['media_id'],'error':i['error']} for i in items if i.get('error')]}
    return result


def add_import_routes(router, settings, connect):
    from app.auth.deps import reader
    from face_detect.recognition import people_review_scope

    @router.get('/selections')
    def selections(user=Depends(reader)):
        with connect() as db:
            return people_review_scope.scanned_imports(db)

    @router.get('/import-status')
    def import_status(user=Depends(reader)):
        return import_progress(settings)


def add_scan_routes(router, settings):
    from app.auth.deps import reader
    from face_detect.api import people_preview_gateway as gateway

    @router.post('/jobs')
    @router.post('/jobs/{name}/control')
    @router.post('/jobs/{name}/recheck')
    @router.post('/jobs/{name}/recheck/stream')
    def unavailable_scan(name:str='',user=Depends(reader)):
        raise HTTPException(409,'Face scanning is not connected to the existing People catalog yet. Saved people remain available for review and editing.')

    if settings.get('native'):
        router.routes[:] = [route for route in router.routes if route.endpoint is not unavailable_scan]

        @router.api_route('/jobs', methods=['POST'])
        @router.api_route('/jobs/{path:path}', methods=['GET', 'POST'])
        async def native_jobs(request: Request, path: str = '', user=Depends(reader)):
            return await gateway.bridge(request, 'jobs'+('/'+path if path else ''), user)


def live_photos(cfg, expected):
    from app.database import connection
    from app.viewer import media_visible
    from face_detect.recognition.faces import VISIBLE
    with connection(cfg['database']) as db:
        rows = db.execute('''SELECT m.*,a.crop,coalesce(a.rotation,0) rotation
            FROM media m LEFT JOIN media_adjustments a ON a.media_id=m.id
            WHERE m.id=ANY(%s) AND m.kind='image' AND ''' + VISIBLE + ' AND ' + media_visible('m') +
            ' ORDER BY m.captured_at DESC NULLS LAST,m.id DESC', (list(expected),)).fetchall()
    return [row for row in rows if row['sha256'] == expected.get(row['id'])]


def person_photo_items(cfg, connect, pid):
    from face_detect.recognition import automatic_people as service
    with connect() as db:
        person = db.execute('SELECT name FROM people WHERE id=%s AND deleted_at IS NULL', (pid,)).fetchone()
        if not person:
            raise HTTPException(404, 'Person unavailable')
        sources = db.execute('SELECT DISTINCT media_id,sha256 FROM (' +
                             service.named_faces_sql() + ') f WHERE person_id=%s', (pid,)).fetchall()
    return {'name': person['name'], 'items': live_photos(cfg, {row['media_id']: row['sha256'] for row in sources})}


def add_photo_routes(router, cfg, connect):
    from app.auth.deps import reader, Identifier
    from face_detect.recognition import automatic_people as service

    @router.get('/groups/{gid}/photos')
    def group_photos(gid: Identifier, user=Depends(reader)):
        with connect() as db:
            try:
                group = service.detail(db, gid)['group']
            except LookupError:
                raise HTTPException(404, 'Group unavailable') from None
            ids = sorted({face['media_id'] for face in group['faces']})
            expected = {row['id']: row['sha256'] for row in db.execute(
                'SELECT id,sha256 FROM media WHERE id=ANY(%s)', (ids,)).fetchall()}
        return {'items': live_photos(cfg, expected)}

    @router.get('/people/{pid}/photos')
    def person_photos(pid: Identifier, user=Depends(reader)):
        return person_photo_items(cfg, connect, pid)


def collection_reader(cfg, settings, connect):
    """Collection links use the same saved identity, not an unavailable HTTP worker."""
    from face_detect.recognition import automatic_people as service
    from starlette.concurrency import run_in_threadpool

    async def collection_read(request, path, pilot=None):
        # A link names the catalog by its own UUID, the same on every computer. The pilot
        # names are accepted too, for links the migration couldn't see.
        link = settings.get('link_pilot', settings['pilot'])
        if pilot is not None and pilot not in (link, settings['pilot'], settings.get('legacy_pilot')):
            raise HTTPException(409, 'This link belongs to a different People catalog')
        def read():
            if path == 'named-people':
                with connect() as db:
                    return service.named_people(db)
            match = re.fullmatch(r'people/([1-9][0-9]*)/photos', path)
            if match:
                return person_photo_items(cfg, connect, int(match[1]))
            raise HTTPException(404, 'Read-only People endpoint unavailable')
        return link, await run_in_threadpool(read)

    return collection_read


def add_thumbnail_routes(router, settings, connect, cfg=None):
    from app.auth.deps import reader, Identifier

    def saved_thumbnail(did, kind):
        with connect() as db:
            row = db.execute(thumbnail_query(kind), (did,)).fetchone()
        if not row:
            raise HTTPException(404, 'Face unavailable')
        folder = settings.get('thumbnails', settings['root'] / 'thumbnails')
        path = (folder / f'{did}.jpg').resolve()
        if not path.is_relative_to(folder):
            raise HTTPException(404, 'Saved thumbnail unavailable in read-only preview')
        if not path.is_file():
            if cfg and cfg.get('recovery', {}).get('activation_required') is True:
                from app.migration.people_thumbnail import recovered_thumbnail
                return recovered_thumbnail(cfg, connect, did, kind)
            # Backups leave face thumbnails out: rebuild a missing one from its photo, and keep it.
            if cfg and settings.get('data_root') and not cfg.get('read_only_preview'):
                from face_detect.catalog.thumbnails import on_demand
                path = on_demand(connect, cfg['destination']['root'], settings['data_root'], did)
            if path is None or not path.is_file():
                raise HTTPException(404, 'Saved thumbnail unavailable in read-only preview')
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @router.get('/thumbnails/{did}')
    def thumbnail(did: Identifier, user=Depends(reader)):
        return saved_thumbnail(did, 'group')

    @router.get('/ignored/{did}/thumbnail')
    def ignored_thumbnail(did: Identifier, user=Depends(reader)):
        return saved_thumbnail(did, 'ignored')

    @router.get('/blurry/{did}/thumbnail')
    def blurry_thumbnail(did: Identifier, user=Depends(reader)):
        return saved_thumbnail(did, 'blurry')


def add_page(app, cfg, settings, root):
    from app.auth.deps import reader

    @app.get('/people-preview')
    def page(user=Depends(reader)):
        html = (root / 'face_detect/web/automatic-people-preview.html').read_text(encoding='utf-8')
        if user['role'] != 'admin':
            html += '<script type="module" src="/web/read-only-preview.mjs"></script>'
        if settings.get('native'):
            html += '<script type="module" src="/web/native-people-catalog.mjs"></script>'
        if cfg.get('live_existing_catalog') and not settings.get('native'):
            html += '<script type="module" src="/web/existing-people-catalog.mjs"></script>'
        elif not cfg.get('live_existing_catalog'):
            script='limited-edit-preview' if cfg.get('limited_metadata_edit') is True else 'read-only-preview'
            html += f'<script type="module" src="/web/{script}.mjs"></script>'
        return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


def install(app, cfg, root):
    from app.database import connection
    from app.albums import collections as album_collections

    settings = catalog_settings(cfg['preview']['data_root'],
                                recovery=cfg.get('recovery', {}).get('activation_required') is True)
    database = catalog_database(cfg, settings)

    def connect():
        return connection(database)

    if settings.get('native'):
        with connect() as db:
            from face_detect.catalog import link_pilot
            settings['link_pilot'] = link_pilot(db)

    apply_live_visibility()
    bind_catalog(cfg, settings, connect)
    drop_network_bridge(app)

    router = APIRouter(prefix='/api/people-preview')
    if settings.get('native'):
        add_native_capabilities(router, connect)
    add_allowlisted_routes(router, cfg)
    add_import_routes(router, settings, connect)
    add_scan_routes(router, settings)
    add_photo_routes(router, cfg, connect)
    album_collections.people_read = collection_reader(cfg, settings, connect)
    add_thumbnail_routes(router, settings, connect, cfg)

    app.include_router(router)
    app.state.existing_people_catalog = True
    add_page(app, cfg, settings, root)
    return settings
