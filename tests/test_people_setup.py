"""Setting up the face engine on a recovered library reuses its People catalog and never makes
a second one. The engine, model download and database are replaced; this tests the order, the
refusals and the exit codes."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.migration import people_setup
from face_detect import catalog
from face_detect.api import service


class Db:
    def execute(self, *args, **kwargs):
        return SimpleNamespace(fetchone=lambda: {'x': 1}, fetchall=lambda: [])


class Calls(list):
    closed = None


@pytest.fixture
def steps(monkeypatch):
    calls = Calls()

    @contextmanager
    def connection(settings=None):
        yield Db()

    monkeypatch.setattr('app.database.connection', connection)
    monkeypatch.setattr('app.config.load_config', lambda: {'database': {}})
    monkeypatch.setattr(catalog, 'exists', lambda db, schema=catalog.SCHEMA: True)
    monkeypatch.setattr(people_setup, 'active_profile', lambda: 'windows_insightface')
    monkeypatch.setattr(people_setup, 'restore_profile', lambda name: calls.append(('restore', name)))
    monkeypatch.setattr('face_detect.cli.setup.setup',
                        lambda provider, research_only=False, activate=False:
                        calls.append(('setup', provider, research_only, activate)) or 'mac_insightface')
    monkeypatch.setattr('face_detect.cli.prepare.prepare',
                        lambda port=None: calls.append(('prepare', port)) or {'catalog': 'adopted', 'space_id': 's'})
    closed = []
    monkeypatch.setattr('face_detect.service.create_service',
                        lambda cfg: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr('face_detect.cli.conformance.check_this_library',
                        lambda svc, cfg: calls.append(('check',)) or {'passed': True, 'reason': 'conforms',
                                                                      'worst': 0.9988, 'median': 1.0})
    monkeypatch.setattr('face_detect.cli.conformance.record_certificate',
                        lambda cfg, result: calls.append(('record', result['passed'])))
    calls.closed = closed
    return calls


def test_installs_the_engine_adopts_the_catalog_then_checks_and_records(steps):
    report = people_setup.run(True, port=54999)
    assert steps == [('setup', 'insightface', True, True), ('prepare', 54999), ('check',), ('record', True)]
    assert steps.closed == [True]
    assert report['profile'] == 'mac_insightface' and report['catalog'] == 'adopted'
    assert report['library_check']['passed'] is True and report['library_check']['worst'] == 0.9988


def test_a_failed_check_is_recorded_and_reported_not_raised(steps, monkeypatch):
    monkeypatch.setattr('face_detect.cli.conformance.check_this_library',
                        lambda svc, cfg: {'passed': False, 'reason': 'vectors-differ', 'worst': 0.5})
    report = people_setup.run(True)
    assert ('record', False) in steps
    assert report['library_check'] == {'passed': False, 'reason': 'vectors-differ', 'worst': 0.5}


def test_skip_check_leaves_it_to_the_first_scan(steps):
    report = people_setup.run(True, check=False)
    assert report['library_check'] == 'skipped'
    assert ('check',) not in steps and not any(call[0] == 'record' for call in steps)


def test_a_library_without_the_catalog_is_refused_before_anything_is_installed(steps, monkeypatch):
    monkeypatch.setattr(catalog, 'exists', lambda db, schema=catalog.SCHEMA: False)
    monkeypatch.setattr(catalog, 'people_schemas', lambda db: ['face_native_' + 'a' * 32])
    with pytest.raises(people_setup.Refused, match='catalog migrate --from face_native_a'):
        people_setup.run(True)
    assert steps == []


def test_a_library_with_no_people_data_at_all_is_refused_too(steps, monkeypatch):
    monkeypatch.setattr(catalog, 'exists', lambda db, schema=catalog.SCHEMA: False)
    monkeypatch.setattr(catalog, 'people_schemas', lambda db: [])
    with pytest.raises(people_setup.Refused, match='no People catalog'):
        people_setup.run(True)
    assert steps == []


def test_the_recovered_folders_are_selected_before_the_work_and_failure_sets_the_exit_code(steps, monkeypatch, capsys):
    order = []
    monkeypatch.setattr(people_setup, 'configure_environment', lambda directory: order.append('environment'))
    monkeypatch.setattr(people_setup, 'run', lambda research_only, port, check=True, thumbnails=False, limit=None:
                        order.append('run') or {'library_check': {'passed': False, 'reason': 'faces-not-found'}})
    assert people_setup.main(['--directory', 'x', '--research-only']) == 1
    assert order == ['environment', 'run']
    assert 'faces-not-found' in capsys.readouterr().out


def test_a_refusal_prints_its_reason_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(people_setup, 'configure_environment', lambda directory: None)

    def refuse(*args, **kwargs):
        raise people_setup.Refused('no catalog')

    monkeypatch.setattr(people_setup, 'run', refuse)
    assert people_setup.main(['--directory', 'x']) == 1
    assert 'Refused: no catalog' in capsys.readouterr().err


def test_scans_start_on_a_recovered_library_that_has_its_catalog(monkeypatch):
    monkeypatch.setattr(service, 'catalog_ready', lambda: True)
    monkeypatch.setattr(service, '_status', {})
    monkeypatch.setattr(service, '_busy', False)
    monkeypatch.setattr(service, '_process', None)
    monkeypatch.setattr('app.config.load_config',
                        lambda: {'recovery': {'state': 'activated', 'native_inference_qualified': False},
                                 'destination': {}})
    monkeypatch.setattr(service.gateway, 'settings', lambda: pytest.fail('Must not touch another service'))
    service.operate('start')
    # It got past the recovery gate and stopped at the next check, the missing destination.
    assert 'first import' in service._status['message']


def test_scans_stay_blocked_on_a_recovered_library_without_a_catalog(monkeypatch):
    monkeypatch.setattr(service, 'catalog_ready', lambda: False)
    monkeypatch.setattr(service, '_status', {})
    monkeypatch.setattr(service, '_busy', False)
    monkeypatch.setattr('app.config.load_config',
                        lambda: {'recovery': {'state': 'activated', 'native_inference_qualified': False}})
    service.operate('start')
    assert service._status['state'] == 'unavailable'
    assert 'people_setup' in service._status['message']


def test_a_refused_model_gives_the_earlier_profile_back(steps, monkeypatch):
    def refuse(port=None):
        raise ValueError("This library's People catalog uses another face model")

    monkeypatch.setattr('face_detect.cli.prepare.prepare', refuse)
    with pytest.raises(people_setup.Refused, match='not the active profile'):
        people_setup.run(True)
    assert ('restore', 'windows_insightface') in steps
    assert ('check',) not in steps


def test_the_report_carries_the_checks_own_counts(steps, monkeypatch):
    monkeypatch.setattr('face_detect.cli.conformance.check_this_library',
                        lambda svc, cfg: {'passed': True, 'reason': 'conforms', 'sampled': 24, 'compared': 24,
                                          'unmatched': 0, 'unreadable': 0, 'worst': 1.0, 'median': 1.0})
    assert people_setup.run(True)['library_check']['compared'] == 24


def test_thumbnails_are_rebuilt_after_the_check_when_asked(steps, monkeypatch):
    monkeypatch.setattr(people_setup, 'rebuild_thumbnails',
                        lambda limit=None: steps.append(('rebuild', limit)) or {'rebuilt': 5})
    report = people_setup.run(True, thumbnails=True, limit=10)
    assert steps[-1] == ('rebuild', 10) and steps.index(('record', True)) < steps.index(('rebuild', 10))
    assert report['thumbnails'] == {'rebuilt': 5}


def test_thumbnails_are_not_rebuilt_unless_asked(steps, monkeypatch):
    monkeypatch.setattr(people_setup, 'rebuild_thumbnails', lambda limit=None: pytest.fail('not asked'))
    assert 'thumbnails' not in people_setup.run(True)


def test_thumbnails_can_be_rebuilt_without_the_check(steps, monkeypatch):
    monkeypatch.setattr(people_setup, 'rebuild_thumbnails', lambda limit=None: {'rebuilt': 1})
    assert people_setup.run(True, check=False, thumbnails=True)['thumbnails'] == {'rebuilt': 1}


def test_the_command_line_passes_the_rebuild_options_on(monkeypatch):
    seen = {}
    monkeypatch.setattr(people_setup, 'configure_environment', lambda directory: None)
    monkeypatch.setattr(people_setup, 'run', lambda research_only, port, check=True, thumbnails=False, limit=None:
                        seen.update(thumbnails=thumbnails, limit=limit) or {})
    assert people_setup.main(['--directory', 'x', '--rebuild-thumbnails', '--limit', '50']) == 0
    assert seen == {'thumbnails': True, 'limit': 50}
