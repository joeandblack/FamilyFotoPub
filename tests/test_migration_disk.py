import hashlib
import json

import psycopg
import pytest

from app.migration import disk
from app.migration import cutover
from app.backup.catalog import verify_catalog
from app.backup.postgres import Endpoint
from tests.test_backup_postgres import postgres, ample  # noqa: F401
from tests.backup_keys import backup_key


@pytest.fixture
def relocation(postgres, tmp_path):
    params, source, _, dump, _ = postgres
    photo = b'fixture photo'
    with psycopg.connect(**{**params, 'dbname': source}) as db:
        db.execute('CREATE TABLE media(relative_path text, size bigint, sha256 text, stored_sha256 text, '
                   'original_relative_path text, original_size bigint, conversion_state text, id integer PRIMARY KEY)')
        db.execute("INSERT INTO media(relative_path,size,sha256,id) VALUES('photo.jpg',13,%s,1)",
                   (hashlib.sha256(photo).hexdigest(),))
        db.execute('CREATE TABLE media_trash(media_id integer, deleting_at timestamptz, deleted_at timestamptz)')
        db.execute('CREATE TABLE video_metadata(media_id integer, metadata jsonb)')
    config, data, media = tmp_path / 'app/config', tmp_path / 'app/data', tmp_path / 'old/media'
    for path in (config, data, media):
        path.mkdir(parents=True)
    (media / 'photo.jpg').write_bytes(photo)

    def probe(path):
        return {'root': str(path), 'drive': 'fixture', 'serial': 'disk',
                'volume_serial': 'volume', 'volume_id': 'fixture-volume'}

    endpoint = Endpoint(params['host'], params['port'], source, params['user'])
    settings = {'database': {'host': endpoint.host, 'port': endpoint.port,
                            'dbname': endpoint.dbname, 'user': endpoint.user},
                'destination': probe(media)}
    (config / 'local.json').write_text(json.dumps(settings))
    (media.parent / '.familyfoto-volume.json').write_text(json.dumps({**probe(media), 'library_id': 'existing-marker'}))
    key, job, backup = backup_key(), tmp_path / 'new-disk-job', tmp_path / 'backup'
    def run(**kwargs):
        return disk.prepare_disk_move(endpoint, dump, config, data, job, backup, key,
            database_budget=1024 ** 2, offline_confirmed=True, guard_factory=ample, probe=probe, **kwargs)
    run.endpoint, run.dump, run.probe = endpoint, dump, probe
    return run, config, data, media, job, backup, key


def test_prepare_disk_move_copies_once_and_never_switches_source(relocation):
    run, config, data, media, job, backup, key = relocation
    before = (config / 'local.json').read_bytes()
    marker = (media.parent / '.familyfoto-volume.json').read_bytes()
    report = run()
    assert report['state'] == 'ready-for-cutover-review'
    assert report['configuration_changed'] is False and report['activation_allowed'] is False
    assert (job / 'copy/media/photo.jpg').read_bytes() == (media / 'photo.jpg').read_bytes()
    assert (config / 'local.json').read_bytes() == before
    assert (media.parent / '.familyfoto-volume.json').read_bytes() == marker
    assert not (job / 'copy/.familyfoto-volume.json').exists()
    assert verify_catalog(backup, key)['id'] == report['capture_id']
    assert not (data / '.recovery-admission/maintenance.json').exists()


@pytest.mark.parametrize('failure', ['copy', 'configuration'])
def test_preparation_failure_never_publishes_ready_or_switches_storage(relocation, monkeypatch, failure):
    run, config, data, media, job, backup, key = relocation
    if failure == 'copy':
        monkeypatch.setattr(disk, 'copy_media_tree', lambda *a, **k: (_ for _ in ()).throw(OSError('Disconnected')))
    else:
        original = disk.copy_media_tree
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            settings = json.loads((config / 'local.json').read_text())
            settings['unexpected_change'] = True
            (config / 'local.json').write_text(json.dumps(settings))
            return result
        monkeypatch.setattr(disk, 'copy_media_tree', changed)
    with pytest.raises((OSError, disk.MaintenanceBlocked)):
        run()
    assert not (job / 'READY.json').exists()
    assert json.loads((config / 'local.json').read_text())['destination']['root'] == str(media)
    assert (media / 'photo.jpg').read_bytes() == b'fixture photo'
    assert (data / '.recovery-admission/maintenance.json').exists()
    assert verify_catalog(backup, key)


def switch_args(relocation):
    run, config, data, media, job, backup, key = relocation
    return (run.endpoint, run.dump, config, data, job, backup.with_name('cutover-backup'), key)


def recover(relocation, **kwargs):
    run, config, data, _, job, _, key = relocation
    return cutover.recover_disk_move(run.endpoint, config, data, job, key,
                                    offline_confirmed=True, probe=run.probe, **kwargs)


def test_cutover_preserves_source_and_refuses_late_rollback(relocation):
    run, config, data, media, job, _, key = relocation
    run()
    report = cutover.commit_disk_move(*switch_args(relocation), database_budget=1024 ** 2,
                                     offline_confirmed=True, guard_factory=ample, probe=run.probe)
    assert report['state'] == 'complete'
    assert json.loads((config / 'local.json').read_text())['destination']['root'] == str(job / 'copy/media')
    assert json.loads((job / 'copy/.familyfoto-volume.json').read_text())['library_id'] == 'existing-marker'
    assert (media / 'photo.jpg').read_bytes() == b'fixture photo'
    assert not (data / '.recovery-admission/maintenance.json').exists()
    with pytest.raises(disk.MaintenanceBlocked, match='already released'):
        recover(relocation, rollback=True)


@pytest.mark.parametrize('stage', ['before-settings', 'after-settings', 'after-report'])
@pytest.mark.parametrize('rollback', [False, True])
def test_interrupted_cutover_can_finish_or_rollback(relocation, monkeypatch, stage, rollback):
    run, config, data, media, job, _, _ = relocation
    old = (config / 'local.json').read_bytes()
    run()
    publish = cutover.replace_settings
    save = cutover.save_private
    def interrupted_publish(path, raw):
        if stage == 'before-settings':
            raise OSError('Disconnected before settings')
        publish(path, raw)
        raise OSError('Disconnected after settings')
    def interrupted_report(path, value):
        save(path, value)
        if path.name == 'CUTOVER-REPORT.json':
            raise OSError('Disconnected after report')
    if stage == 'after-report':
        monkeypatch.setattr(cutover, 'save_private', interrupted_report)
    else:
        monkeypatch.setattr(cutover, 'replace_settings', interrupted_publish)
    with pytest.raises(OSError, match='Disconnected'):
        cutover.commit_disk_move(*switch_args(relocation), database_budget=1024 ** 2,
                                 offline_confirmed=True, guard_factory=ample, probe=run.probe)
    assert (data / '.recovery-admission/maintenance.json').exists()
    monkeypatch.setattr(cutover, 'replace_settings', publish)
    monkeypatch.setattr(cutover, 'save_private', save)
    result = recover(relocation, rollback=rollback)
    assert result['state'] == ('rolled-back' if rollback else 'complete')
    if rollback:
        assert (config / 'local.json').read_bytes() == old
    else:
        assert json.loads((config / 'local.json').read_text())['destination']['root'] == str(job / 'copy/media')
    assert (media / 'photo.jpg').read_bytes() == b'fixture photo'
    assert not (data / '.recovery-admission/maintenance.json').exists()


def test_cutover_refuses_modified_source(relocation):
    run, config, data, media, job, _, _ = relocation
    run()
    (media / 'photo.jpg').write_bytes(b'changed photo')
    with pytest.raises(ValueError):
        cutover.commit_disk_move(*switch_args(relocation), database_budget=1024 ** 2,
                                 offline_confirmed=True, guard_factory=ample, probe=run.probe)
    assert not (job / 'cutover-intent').exists()
    assert json.loads((config / 'local.json').read_text())['destination']['root'] == str(media)


def test_recovery_refuses_foreign_settings_without_releasing_gate(relocation, monkeypatch):
    run, config, data, _, _, _, _ = relocation
    run()
    publish = cutover.replace_settings
    monkeypatch.setattr(cutover, 'replace_settings', lambda *a: (_ for _ in ()).throw(OSError('Interrupted')))
    with pytest.raises(OSError):
        cutover.commit_disk_move(*switch_args(relocation), database_budget=1024 ** 2,
                                 offline_confirmed=True, guard_factory=ample, probe=run.probe)
    monkeypatch.setattr(cutover, 'replace_settings', publish)
    foreign = (config / 'local.json').read_bytes() + b' '
    (config / 'local.json').write_bytes(foreign)
    with pytest.raises(disk.MaintenanceBlocked, match='independently changed'):
        recover(relocation, rollback=True)
    assert (config / 'local.json').read_bytes() == foreign
    assert (data / '.recovery-admission/maintenance.json').exists()


def test_preparation_resume_reuses_published_media(relocation, monkeypatch):
    from app.backup import media_tree
    run, config, data, media, job, _, _ = relocation
    def interrupt(event):
        if event.get('phase') == 'copy':
            raise OSError('Interrupted after publishing media')
    with pytest.raises(OSError, match='Interrupted'):
        run(progress=interrupt)
    published = job / 'copy/media/photo.jpg'
    assert published.read_bytes() == b'fixture photo'
    before = published.stat().st_mtime_ns
    monkeypatch.setattr(media_tree, 'copy_media_file', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('Published media must not be recopied')))
    result = run(resume=True)
    assert result['state'] == 'ready-for-cutover-review'
    assert published.stat().st_mtime_ns == before
    assert json.loads((config / 'local.json').read_text())['destination']['root'] == str(media)
    assert not (data / '.recovery-admission/maintenance.json').exists()
    with pytest.raises(disk.MaintenanceBlocked, match='already released'):
        run(resume=True)


def test_preparation_resume_refuses_changed_settings(relocation):
    run, config, data, _, _, _, _ = relocation
    def interrupt(event):
        if event.get('phase') == 'copy':
            raise OSError('Interrupted')
    with pytest.raises(OSError):
        run(progress=interrupt)
    with (config / 'local.json').open('ab') as output:
        output.write(b' ')
    with pytest.raises(disk.MaintenanceBlocked, match='ownership changed'):
        run(resume=True)
    assert (data / '.recovery-admission/maintenance.json').exists()
