import json

import pytest
from cryptography.fernet import Fernet

from app.backup.media_copy import copy_lock
from fastbackup import capture, cleanup
from fastbackup.tests.test_full_capture import world, run  # noqa: F401


@pytest.fixture
def incomplete(world, monkeypatch):  # noqa: F811
    def fail(source, inventory, target, **kwargs):
        target.mkdir()
        (target / 'media').mkdir()
        (target / 'media/image.jpg').write_bytes(b'im')
        raise OSError('injected copy failure')
    monkeypatch.setattr(capture, 'copy_in_worker', fail)
    with pytest.raises(OSError, match='injected'):
        run(world)
    return world


def test_preview_then_delete_payload_only(incomplete):
    w = incomplete
    owner = (w.backup / 'capture-job.json').read_bytes()
    plan = cleanup.cleanup_incomplete(w.backup, w.key)
    assert plan['files'] and plan['bytes'] and (w.backup / 'media-copy/media/image.jpg').exists()
    with pytest.raises(ValueError, match='preview changed'):
        cleanup.cleanup_incomplete(w.backup, w.key, confirm=True, expected_tree='wrong')
    result = cleanup.cleanup_incomplete(w.backup, w.key, confirm=True, expected_tree=plan['expected_tree'])
    assert result['deleted_files'] == plan['files']
    assert (w.backup / 'capture-job.json').read_bytes() == owner
    assert (w.media / 'image.jpg').read_bytes() == b'image'
    assert w.backup.is_dir() and (w.backup / 'media-copy/media').is_dir()
    assert cleanup.cleanup_incomplete(w.backup, w.key)['files'] == 0


def test_completed_capture_refused(world):  # noqa: F811
    run(world)
    with pytest.raises(ValueError, match='Completed'):
        cleanup.cleanup_incomplete(world.backup, world.key)
    assert (world.backup / 'media-copy/media/image.jpg').read_bytes() == b'image'


@pytest.mark.parametrize('change', ['new-file', 'changed-file', 'unknown-root', 'copied-owner', 'legacy', 'hardlink'])
def test_changed_or_unowned_cleanup_refused(incomplete, change):
    w = incomplete
    plan = cleanup.cleanup_incomplete(w.backup, w.key)
    target = w.backup / 'media-copy/media/image.jpg'
    if change == 'new-file':
        (target.parent / 'new').write_bytes(b'keep')
    elif change == 'changed-file':
        target.write_bytes(b'changed')
    elif change == 'unknown-root':
        (w.backup / 'personal-file').write_bytes(b'keep')
    elif change in {'copied-owner', 'legacy'}:
        record = w.backup / 'capture-job.json'
        job = json.loads(Fernet(w.key).decrypt(json.loads(record.read_text())['sealed'].encode()))
        if change == 'copied-owner':
            job['roots']['backup']['identity'][1] += 1
        else:
            del job['engine']
        record.write_text(json.dumps({'sealed': Fernet(w.key).encrypt(json.dumps(job).encode()).decode()}))
    else:
        (target.parent / 'linked').hardlink_to(target)
    with pytest.raises(ValueError):
        cleanup.cleanup_incomplete(w.backup, w.key, confirm=True, expected_tree=plan['expected_tree'])
    assert target.exists()


def test_active_attempt_refused(incomplete):
    with copy_lock(incomplete.backup), pytest.raises(OSError):
        cleanup.cleanup_incomplete(incomplete.backup, incomplete.key)


@pytest.mark.parametrize('folder', ['restore-on-mac', 'restore-on-windows'])
def test_failed_capture_with_handover_can_be_cleaned(incomplete, folder):
    handover = incomplete.backup / folder
    handover.mkdir()
    (handover / 'README.txt').write_text('Restore instructions')
    plan = cleanup.cleanup_incomplete(incomplete.backup, incomplete.key)
    cleanup.cleanup_incomplete(incomplete.backup, incomplete.key, confirm=True,
                               expected_tree=plan['expected_tree'])
    assert (handover / 'README.txt').exists()


def test_active_copy_worker_refused_even_without_parent_lock(incomplete):
    with copy_lock(incomplete.backup / 'media-copy'), pytest.raises(OSError):
        cleanup.cleanup_incomplete(incomplete.backup, incomplete.key)


def test_interrupted_cleanup_can_be_replanned(incomplete, monkeypatch):
    w = incomplete
    plan = cleanup.cleanup_incomplete(w.backup, w.key)
    actual = cleanup.remove_file
    calls = []
    def fail(*args):
        if calls:
            raise OSError('injected delete failure')
        calls.append(True)
        return actual(*args)
    monkeypatch.setattr(cleanup, 'remove_file', fail)
    with pytest.raises(OSError, match='injected'):
        cleanup.cleanup_incomplete(w.backup, w.key, confirm=True, expected_tree=plan['expected_tree'])
    monkeypatch.setattr(cleanup, 'remove_file', actual)
    next_plan = cleanup.cleanup_incomplete(w.backup, w.key)
    assert next_plan['files'] == plan['files'] - 1
    cleanup.cleanup_incomplete(w.backup, w.key, confirm=True, expected_tree=next_plan['expected_tree'])
    assert cleanup.cleanup_incomplete(w.backup, w.key)['files'] == 0


def test_linked_directory_refused(incomplete):
    target = incomplete.backup / 'media-copy/linked'
    try:
        target.symlink_to(incomplete.media, target_is_directory=True)
    except OSError:
        pytest.skip('Symlink creation unavailable for this Windows token')
    with pytest.raises((ValueError, OSError)):
        cleanup.cleanup_incomplete(incomplete.backup, incomplete.key)
    assert (incomplete.media / 'image.jpg').read_bytes() == b'image'


def test_cli_preview_requires_matching_confirmation(incomplete, monkeypatch, capsys):
    from fastbackup import __main__ as cli
    monkeypatch.setattr(cli.passwords, 'from_arguments', lambda args: 'fixture-password')
    monkeypatch.setattr(cli.passwords, 'unlock_context', lambda *args: incomplete.key)
    args = ['cleanup-incomplete', '--directory', str(incomplete.backup)]
    assert cli.main(args) == 0
    plan = json.loads(capsys.readouterr().out)['result']
    assert cli.main(args + ['--confirm-delete']) == 1
    capsys.readouterr()
    assert cli.main(args + ['--confirm-delete', '--expected-tree', plan['expected_tree']]) == 0
    assert json.loads(capsys.readouterr().out)['result']['deleted_files'] == plan['files']


def test_unknown_directory_is_not_even_given_a_lock(tmp_path):
    from tests.backup_keys import backup_key
    with pytest.raises((OSError, ValueError)):
        cleanup.cleanup_incomplete(tmp_path, backup_key())
    assert not (tmp_path / 'copy.lock').exists()


def test_appledouble_control_files_kept_and_payload_sidecars_reclaimed(incomplete):
    from app.backup.sidecars import APPLE_DOUBLE
    w = incomplete
    content = APPLE_DOUBLE + b'fixture metadata'
    control = w.backup / '._capture-job.json'
    orphan = w.backup / '._old-control'
    payload = w.backup / 'media-copy/media/._image.jpg'
    for path in (control, orphan, payload):
        path.write_bytes(content)
    plan = cleanup.cleanup_incomplete(w.backup, w.key)
    result = cleanup.cleanup_incomplete(w.backup, w.key, confirm=True, expected_tree=plan['expected_tree'])
    assert result['deleted_files'] == plan['files']
    assert control.read_bytes() == orphan.read_bytes() == content
    assert not payload.exists()
    assert (w.media / 'image.jpg').read_bytes() == b'image'
    assert cleanup.cleanup_incomplete(w.backup, w.key)['files'] == 0


@pytest.mark.parametrize('kind', ['lookalike', 'hardlink'])
def test_control_sidecar_does_not_hide_unowned_file(incomplete, kind):
    from app.backup.sidecars import APPLE_DOUBLE
    w = incomplete
    sidecar = w.backup / '._capture-job.json'
    if kind == 'hardlink':
        outside = w.data / 'metadata-to-keep'
        outside.write_bytes(APPLE_DOUBLE + b'keep')
        sidecar.hardlink_to(outside)
    else:
        sidecar.write_bytes(b'not AppleDouble')
    with pytest.raises(ValueError):
        cleanup.cleanup_incomplete(w.backup, w.key)
    assert (w.backup / 'media-copy/media/image.jpg').read_bytes() == b'im'
    assert sidecar.exists()
