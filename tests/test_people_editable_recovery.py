"""A recovered library can edit People once this computer is set up for its catalog, and only
views them before. The routes, the launcher's decision and the check behind it."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

from fastapi import APIRouter
import pytest

from app.migration import launch
from face_detect import catalog
from face_detect.api import automatic_people_api as api
from face_detect.api import readonly_people
from face_detect.catalog import certify


def post_suffixes(router):
    return {route.path.removeprefix('/api/people-preview') for route in router.routes
            if getattr(route, 'methods', None) == {'POST'}}


def edit_routes():
    return {route.path.removeprefix('/api/automatic-people') for route in api.router.routes
            if route.methods == {'POST'}}


def test_a_recovered_library_routes_no_edit_until_it_is_set_up():
    router = APIRouter(prefix='/api/people-preview')
    readonly_people.add_allowlisted_routes(router, {'preview': {'people_readonly': True}})
    assert post_suffixes(router) == set()


def test_once_set_up_every_edit_route_the_live_library_has_is_routed():
    router = APIRouter(prefix='/api/people-preview')
    readonly_people.add_allowlisted_routes(router, {'live_existing_catalog': True})
    assert edit_routes()  # naming, review, moves, Ignore/Restore, Undo are among them
    assert post_suffixes(router) == edit_routes()


@pytest.fixture
def launched(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'people-preview.json').write_text(json.dumps({'enabled': True}))
    monkeypatch.setenv('FAMILYFOTO_DATA_DIR', str(data))
    seen = {}
    cfg = {'recovery': {'state': 'activated', 'activation_required': False}}
    monkeypatch.setattr(launch, 'configure_environment', lambda directory: cfg)
    monkeypatch.setattr('face_detect.api.readonly_people.install',
                        lambda app, preview, root: seen.update(preview=preview))
    monkeypatch.setattr('app.service.desktop.main', lambda: seen.update(served=True))
    return seen


def test_launch_keeps_people_view_only_before_this_computer_is_set_up(launched, monkeypatch):
    monkeypatch.setattr(launch, 'editable_people', lambda cfg: False)
    launch.main(['--directory', 'x'])
    assert launched['served'] is True
    assert launched['preview']['preview']['people_readonly'] is True
    assert not launched['preview'].get('live_existing_catalog')


def test_launch_lets_people_be_edited_once_it_is_set_up(launched, monkeypatch):
    monkeypatch.setattr(launch, 'editable_people', lambda cfg: True)
    launch.main(['--directory', 'x'])
    assert launched['preview']['live_existing_catalog'] is True
    assert launched['preview']['preview']['people_readonly'] is False
    # The recovered library's own settings are not changed, only the copy People is given.
    assert launched['preview']['recovery']['activation_required'] is True


class Provider:
    space_id = 'space'

    def close(self):
        pass


@pytest.fixture
def checked(monkeypatch):
    @contextmanager
    def connect(**kwargs):
        yield SimpleNamespace(execute=lambda *a, **k: None)

    monkeypatch.setattr('psycopg.connect', connect)
    monkeypatch.setattr(catalog, 'database', lambda cfg: {})
    monkeypatch.setattr(catalog, 'exists', lambda db, schema=catalog.SCHEMA: True)
    monkeypatch.setattr('face_detect.catalog.spaces.active_space', lambda db: 'space')
    monkeypatch.setattr('face_detect.service.create_service', lambda cfg: Provider())
    monkeypatch.setattr(certify, 'needs_check', lambda db, provider: None)


def test_this_computer_may_write_when_the_space_matches_and_it_passed(checked):
    assert certify.this_computer_may_write({}) is True


def test_this_computer_may_not_write_before_its_check_has_passed(checked, monkeypatch):
    monkeypatch.setattr(certify, 'needs_check', lambda db, provider: 'not checked yet')
    assert certify.this_computer_may_write({}) is False


def test_this_computer_may_not_write_with_another_model(checked, monkeypatch):
    monkeypatch.setattr('face_detect.catalog.spaces.active_space', lambda db: 'other')
    assert certify.this_computer_may_write({}) is False


def test_this_computer_may_not_write_without_a_catalog_or_an_engine(checked, monkeypatch):
    monkeypatch.setattr(catalog, 'exists', lambda db, schema=catalog.SCHEMA: False)
    assert certify.this_computer_may_write({}) is False

    def no_engine(cfg):
        raise ValueError('no active profile')

    monkeypatch.setattr('face_detect.service.create_service', no_engine)
    assert certify.this_computer_may_write({}) is False


def test_the_reason_it_may_not_write_is_a_sentence(checked, monkeypatch):
    assert certify.write_blocker({}) is None
    monkeypatch.setattr('face_detect.catalog.spaces.active_space', lambda db: 'other')
    assert 'not the library\'s active model' in certify.write_blocker({})
    monkeypatch.setattr(catalog, 'exists', lambda db, schema=catalog.SCHEMA: False)
    assert 'no People catalog' in certify.write_blocker({})
    monkeypatch.setattr('face_detect.service.create_service', lambda cfg: 1 / 0)
    assert 'not set up' in certify.write_blocker({})


def test_launch_says_why_people_is_view_only(monkeypatch, capsys):
    monkeypatch.setattr('face_detect.catalog.certify.write_blocker',
                        lambda cfg: 'this computer has not been checked against the library yet')
    assert launch.editable_people({}) is False
    assert 'People is view-only: this computer has not been checked' in capsys.readouterr().err
    monkeypatch.setattr('face_detect.catalog.certify.write_blocker', lambda cfg: None)
    assert launch.editable_people({}) is True
    assert capsys.readouterr().err == ''
