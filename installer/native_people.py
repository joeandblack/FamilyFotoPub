"""Shared read-only checks for native People catalogs: a fresh one, or one an install adopts.

A library has one People catalog, `people`. A fresh install creates it empty. An install on
a library that already has one (restored from another computer) adopts it, with its people,
groups and matching policy, if this computer's model is the same space. The policy only
applies to this computer once its library check passes; that's checked when it scans.
"""
import json
import subprocess
from pathlib import Path

def check_empty_people_schemas(cfg):
    """A library with the one catalog is adopted. One with only older per-model schemas must
    be migrated first: setup never adopts those."""
    import psycopg
    with psycopg.connect(**cfg['database'], connect_timeout=5) as db:
        db.execute('SET TRANSACTION READ ONLY')
        if db.execute("SELECT 1 FROM pg_namespace WHERE nspname='people'").fetchone():
            return 'adopt'
        if db.execute("SELECT 1 FROM pg_namespace WHERE starts_with(nspname, 'face_native_') "
                      "OR starts_with(nspname, 'auto_eval_') LIMIT 1").fetchone():
            raise ValueError('Existing People schemas found; run the catalog migration first, setup will not adopt them')
        return 'create'


def verify_provider_paths(root, profile):
    base = (root / 'runtime/face_detect').resolve()
    executable = Path(profile['python'])
    # POSIX venv executables normally symlink to the system interpreter.
    if not executable.parent.resolve().is_relative_to(base) or not Path(profile['model_manifest']).resolve().is_relative_to(base):
        raise ValueError('Provider assets must belong to this installation')
    observed = subprocess.check_output([str(executable), '-I', '-c', 'import sys; print(sys.prefix)'], text=True, timeout=15).strip()
    if Path(observed).resolve() != executable.parent.parent.resolve():
        raise ValueError('Provider interpreter does not belong to its private environment')


def verify(root, cfg):
    from face_detect.configuration import resolve
    from face_detect.service import create_service
    _, profile = resolve(cfg)
    verify_provider_paths(root, profile)
    service = create_service(cfg)
    try:
        service.observe()
        if service.manifest['matching'].get('calibrated') or service.manifest['matching'].get('mode') != 'review-only':
            raise ValueError('Fresh installations must not inherit calibrated policies')
        token = service.identity[:32]
        runtime = json.loads((root / f'data/face_detect/profiles/{token}/runtime.json').read_text())
        gateway = json.loads((root / 'data/people-preview.json').read_text())
        if runtime['model_id'] != service.identity or runtime['schema'] != 'people':
            raise ValueError('People catalog identity mismatch')
        if gateway.get('pilot') != token or gateway.get('backend') != 'native':
            raise ValueError('People gateway identity mismatch')
        preferences = json.loads((root / 'data/face-service-settings.json').read_text())
        if preferences != {'start_with_app': False}:
            raise ValueError('Fresh setup must leave automatic service startup disabled')
        import psycopg
        from psycopg import sql
        with psycopg.connect(**cfg['database'], connect_timeout=5) as db:
            db.execute('SET TRANSACTION READ ONLY')
            active = db.execute('SELECT active_space_id FROM people.catalog_state WHERE singleton').fetchone()
            faces = db.execute('SELECT EXISTS(SELECT 1 FROM people.face_detections)').fetchone()[0]
            if active is None and not faces:
                # A fresh catalog: nothing inherited.
                for table in ('policy', 'people', 'auto_people_groups'):
                    if db.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM people.{})').format(sql.Identifier(table))).fetchone()[0]:
                        raise ValueError('Fresh installations must not inherit matching policies' if table == 'policy'
                                         else 'People catalog is not empty')
                adopted = False
            else:
                # An adopted catalog keeps its people and policy, but only for this model's space.
                if active is None or active[0] != service.space_id:
                    raise ValueError("This library's People catalog uses another face model")
                adopted = True
        return {'provider': profile['provider'], 'model_id': service.identity,
                'catalog': 'adopted' if adopted else 'created',
                'port': gateway['port'], 'state': 'installed-not-started',
                'matching': 'review-only', 'start_with_app': False}
    finally:
        service.close()
