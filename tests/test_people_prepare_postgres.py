"""Preparing People on a recovered library whose catalog has no faces yet records this
computer's model, so the first scan can start and People can be edited. Found on Windows:
a Mac library backed up before its first scan, restored with People on, refused Start with
"Set up the face engine on this computer" even after setup had run. Real PostgreSQL, on the
opt-in disposable backup-plan-pg cluster only; every row is synthetic."""
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from app.database.schema_upgrading import migrate
from face_detect import catalog
from face_detect.api import service
from face_detect.catalog import certify
from face_detect.catalog.spaces import active_space
from face_detect.cli import prepare as prepare_module

ROOT = Path(__file__).resolve().parents[1]


def provider(space='space-this-computer', detector='detector-this-computer'):
    return SimpleNamespace(identity='a' * 64, space_id=space, detector_id=detector,
                           manifest={'device': 'cpu', 'runtime_versions': {'onnxruntime': 'test'}},
                           close=lambda: None)


@pytest.fixture
def library(monkeypatch, tmp_path):
    """A migrated library database with an empty People catalog, as a backup made before the
    first scan restores it, and prepare() pointed at it."""
    port = os.environ.get('FAMILYFOTO_BACKUP_TEST_PORT')
    if not port:
        pytest.skip('Requires explicitly started disposable backup-plan-pg cluster')
    assert int(port) != 5432
    params = dict(host='127.0.0.1', port=int(port), user='backup_test')
    name = 'people_prepare_' + uuid.uuid4().hex
    with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
        actual = Path(admin.execute('SHOW data_directory').fetchone()[0]).resolve()
        assert actual == (ROOT / 'temp/backup-plan-pg').resolve(), 'Not the disposable cluster'
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
    settings = {**params, 'dbname': name}
    try:
        def connect():
            return psycopg.connect(**settings, row_factory=dict_row)
        migrate(connect=connect, directory=ROOT / 'databases', before_upgrade=None)
        with connect() as db:
            catalog.create(db)

        @contextmanager
        def connection(cfg=None):
            with connect() as db:
                yield db

        cfg = {'database': settings}
        this = provider()
        monkeypatch.setattr(prepare_module.database, 'connection', connection)
        monkeypatch.setattr(prepare_module, 'load_config', lambda: cfg)
        monkeypatch.setattr(prepare_module, 'resolve', lambda c: ('windows_insightface', {'provider': 'insightface'}))
        monkeypatch.setattr(prepare_module, 'create_service', lambda c: this)
        monkeypatch.setattr(prepare_module, 'DATA', tmp_path)
        monkeypatch.setattr('app.database.connection', connection)
        monkeypatch.setattr('face_detect.service.create_service', lambda c: this)
        yield SimpleNamespace(connect=connect, cfg=cfg, provider=this)
    finally:
        with psycopg.connect(**params, dbname='postgres', autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def catalog_rows(library):
    with library.connect() as db:
        db.execute("SELECT set_config('search_path', %s, true)", (catalog.search_path(),))
        return (db.execute('SELECT active_space_id FROM catalog_state').fetchall(),
                db.execute('SELECT space_id, state FROM space').fetchall(),
                db.execute('SELECT detector_id FROM detector').fetchall(),
                active_space(db))


def test_the_restore_situation_is_blocked_before_prepare(library):
    # The two checks the owner met: Start is refused, and People is view-only.
    assert service.catalog_ready() is False
    assert certify.write_blocker(library.cfg) == "this computer's face model is not the library's active model"


def test_an_empty_adopted_catalog_gets_this_computers_model(library):
    report = prepare_module.prepare(54999)
    assert report['catalog'] == 'adopted'
    states, spaces, detectors, active = catalog_rows(library)
    assert states == [{'active_space_id': 'space-this-computer'}]
    assert spaces == [{'space_id': 'space-this-computer', 'state': 'active'}]
    assert detectors == [{'detector_id': 'detector-this-computer'}]
    assert active == 'space-this-computer'
    # Start is no longer refused, and People can be edited: nothing to check against yet.
    assert service.catalog_ready() is True
    assert certify.write_blocker(library.cfg) is None


def test_preparing_again_changes_nothing(library):
    prepare_module.prepare(54999)
    first = catalog_rows(library)
    prepare_module.prepare(54999)
    assert catalog_rows(library) == first


def test_a_catalog_with_another_model_is_still_refused_and_left_alone(library):
    with library.connect() as db:
        db.execute("SELECT set_config('search_path', %s, true)", (catalog.search_path(),))
        from face_detect.catalog.spaces import register_space
        register_space(db, provider('space-other-computer', 'detector-other-computer'))
    before = catalog_rows(library)
    with pytest.raises(ValueError, match='uses another face model'):
        prepare_module.prepare(54999)
    assert catalog_rows(library) == before
    assert before[3] == 'space-other-computer'


def test_a_new_catalog_is_still_left_for_the_first_scan(library):
    # A fresh install creates the catalog; its model is still recorded by the first scan.
    with library.connect() as db:
        db.execute('DROP SCHEMA people CASCADE')
    assert prepare_module.prepare(54999)['catalog'] == 'created'
    states, spaces, _, active = catalog_rows(library)
    assert states == [] and spaces == [] and active is None
