import hashlib

import pytest

from app.backup import media_tree
from app.backup.media_inventory import capture_media_inventory, InvalidMediaInventory


class Guard:
    def __init__(self, requirements):
        self.requirements = requirements

    def check(self, **kwargs):
        pass

    def written(self, *args):
        pass


def fixture_tree(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'empty').mkdir()
    (source / 'trash').mkdir()
    (source / 'a.jpg').write_bytes(b'photo')
    (source / 'trash/b.MTS').write_bytes(b'video-original')
    inventory = tmp_path / 'inventory'
    capture_media_inventory(source, inventory, offline_confirmed=True, guard_factory=Guard)
    return source, inventory, tmp_path / 'target'


def copy(source, inventory, target, **kwargs):
    return media_tree.copy_media_tree(source, inventory, target, offline_confirmed=True,
                                     guard_factory=Guard, **kwargs)


def test_complete_tree_preserves_source_and_verifies_each_copy(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    result = copy(source, inventory, target)
    assert result['files'] == 2
    assert result['bytes'] == 19
    assert result['content_verified'] is True
    assert result['activation_allowed'] is False
    assert (target / 'media/empty').is_dir()
    assert (target / 'media/trash/b.MTS').read_bytes() == b'video-original'
    assert (source / 'trash/b.MTS').read_bytes() == b'video-original'
    assert not (target / 'TREE-INCOMPLETE').exists()
    assert (target / 'inventory/inventory.sqlite3').is_file()
    assert copy(source, inventory, target) == result
    source.rename(tmp_path / 'disconnected-source')
    assert media_tree.verify_media_tree(target)['content_verified']


def test_a_finished_copy_keeps_no_per_file_job_folders(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    copy(source, inventory, target)
    assert list((target / 'file-jobs').iterdir()) == []


def test_a_job_folder_that_cannot_be_removed_does_not_fail_the_copy(tmp_path, monkeypatch):
    source, inventory, target = fixture_tree(tmp_path)
    real = media_tree.Path.rmdir

    def refuse(path):
        if path.parent.name == 'file-jobs':
            raise PermissionError('in use')
        return real(path)
    monkeypatch.setattr(media_tree.Path, 'rmdir', refuse)
    assert copy(source, inventory, target)['files'] == 2
    assert media_tree.verify_media_tree(target)['files'] == 2


def test_the_copy_leaves_rereading_the_whole_tree_to_verification(tmp_path, monkeypatch):
    source, inventory, target = fixture_tree(tmp_path)
    monkeypatch.setattr(media_tree, 'hash_file', lambda *a: pytest.fail('Reread belongs to verification'))
    assert copy(source, inventory, target)['content_verified'] is True


def test_the_copy_still_refuses_a_tree_of_the_wrong_shape(tmp_path, monkeypatch):
    source, inventory, target = fixture_tree(tmp_path)
    real = media_tree.remove_finished_job

    def intruder(job):
        (target / 'media/intruder').write_bytes(b'x')
        real(job)
    monkeypatch.setattr(media_tree, 'remove_finished_job', intruder)
    with pytest.raises(InvalidMediaInventory, match='Unexpected'):
        copy(source, inventory, target)
    assert (target / 'TREE-INCOMPLETE').exists()


def test_the_copy_still_refuses_a_file_of_the_wrong_size(tmp_path, monkeypatch):
    source, inventory, target = fixture_tree(tmp_path)
    real = media_tree.remove_finished_job
    seen = []

    def shorten(job):
        seen.append(job)
        real(job)
        if len(seen) == 2:
            (target / 'media/a.jpg').write_bytes(b'x')
    monkeypatch.setattr(media_tree, 'remove_finished_job', shorten)
    with pytest.raises(InvalidMediaInventory, match='verification'):
        copy(source, inventory, target)
    assert (target / 'TREE-INCOMPLETE').exists()


def test_verification_reports_progress_with_totals(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    copy(source, inventory, target)
    events = []
    media_tree.verify_media_tree(target, events.append)
    assert [event['files'] for event in events] == [1, 2]
    assert events[-1] == {'phase': 'verify', 'files': 2, 'total_files': 2, 'bytes': 19, 'total_bytes': 19}


def test_resume_after_file_publication_does_not_recopy_completed_files(tmp_path, monkeypatch):
    source, inventory, target = fixture_tree(tmp_path)

    def interrupted(event):
        if event['phase'] == 'copy':
            raise OSError('disconnect')

    with pytest.raises(OSError, match='disconnect'):
        copy(source, inventory, target, progress=interrupted)
    assert (target / 'TREE-INCOMPLETE').exists()
    assert not (target / 'TREE-COMPLETE.json').exists()
    calls = []
    original = media_tree.copy_media_file

    def counting(path, *args, **kwargs):
        calls.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(media_tree, 'copy_media_file', counting)
    assert copy(source, inventory, target)['content_verified']
    assert calls == ['b.MTS']


def test_resume_mid_file_uses_verified_prefix(tmp_path, monkeypatch):
    source, inventory, target = fixture_tree(tmp_path)
    original = media_tree.copy_media_file

    def interrupt(path, job, digest, **kwargs):
        def stop(*args):
            raise OSError('power loss')
        return original(path, job, digest, chunk_size=2, progress=stop, **kwargs)

    monkeypatch.setattr(media_tree, 'copy_media_file', interrupt)
    with pytest.raises(OSError, match='power loss'):
        copy(source, inventory, target)
    job = target / 'file-jobs' / hashlib.sha256(b'a.jpg').hexdigest()
    assert (job / 'payload.part').stat().st_size == 2
    monkeypatch.setattr(media_tree, 'copy_media_file', original)
    assert copy(source, inventory, target)['files'] == 2


def test_corruption_or_extra_output_prevents_completion(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    copy(source, inventory, target)
    (target / 'media/a.jpg').write_bytes(b'bad!!')
    with pytest.raises(InvalidMediaInventory, match='verification'):
        media_tree.verify_media_tree(target)
    with pytest.raises(InvalidMediaInventory, match='damaged'):
        copy(source, inventory, target)
    assert (target / 'TREE-INCOMPLETE').exists()
    with pytest.raises(InvalidMediaInventory, match='incomplete'):
        media_tree.verify_media_tree(target)


def test_refuses_foreign_target_and_nested_output(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    target.mkdir()
    with pytest.raises(InvalidMediaInventory, match='Unowned'):
        copy(source, inventory, target)
    (target / 'keep.txt').write_text('keep')
    with pytest.raises(InvalidMediaInventory, match='Unowned'):
        copy(source, inventory, target)
    assert (target / 'keep.txt').read_text() == 'keep'
    with pytest.raises(ValueError, match='separate'):
        copy(source, inventory, source / 'backup')


def test_verify_rejects_unlisted_file(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    copy(source, inventory, target)
    (target / 'media/extra').write_bytes(b'extra')
    with pytest.raises(InvalidMediaInventory, match='Unexpected'):
        media_tree.verify_media_tree(target)


def test_retry_recovers_interrupted_completion_report(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    copy(source, inventory, target)
    (target / 'TREE-INCOMPLETE').touch()
    (target / 'TREE-COMPLETE.json.part').write_bytes(b'{partial')
    assert copy(source, inventory, target)['content_verified']
    assert not (target / 'TREE-COMPLETE.json.part').exists()


def test_retry_after_atomic_report_publication_failure(tmp_path, monkeypatch):
    from app.backup import recovery
    source, inventory, target = fixture_tree(tmp_path)
    replace = recovery.os.replace

    def interrupt(source_path, destination):
        if destination == target / 'TREE-COMPLETE.json':
            raise OSError('Interrupted report publication')
        return replace(source_path, destination)

    monkeypatch.setattr(recovery.os, 'replace', interrupt)
    with pytest.raises(OSError, match='publication'):
        copy(source, inventory, target)
    attempts = list(target.glob('TREE-COMPLETE.json.*.part'))
    assert len(attempts) == 1
    raw = attempts[0].read_bytes()
    monkeypatch.setattr(recovery.os, 'replace', replace)
    monkeypatch.setattr(media_tree, 'copy_media_file', lambda *a, **k: pytest.fail('Completed media recopied'))
    assert copy(source, inventory, target)['content_verified']
    assert attempts[0].read_bytes() == raw


@pytest.mark.parametrize('name', ['unrelated.' + 'a' * 32 + '.part', 'TREE-COMPLETE.json.not-a-uuid.part'])
def test_retry_rejects_unowned_temporary_filename(tmp_path, name):
    source, inventory, target = fixture_tree(tmp_path)
    copy(source, inventory, target)
    (target / name).write_text('{}')
    with pytest.raises(InvalidMediaInventory, match='Unowned'):
        copy(source, inventory, target)


def test_verify_foreign_directory_does_not_create_lock(tmp_path):
    with pytest.raises(InvalidMediaInventory, match='ownership'):
        media_tree.verify_media_tree(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('changed_root', ['source', 'target'])
def test_resume_refuses_reused_path_on_different_volume(tmp_path, monkeypatch, changed_root):
    source, inventory, target = fixture_tree(tmp_path)
    probe = media_tree.volume_identity
    def stop(event):
        if event['phase'] == 'copy':
            raise OSError('disconnect')
    with pytest.raises(OSError, match='disconnect'):
        copy(source, inventory, target, progress=stop)
    changed = source if changed_root == 'source' else target
    monkeypatch.setattr(media_tree, 'volume_identity', lambda path: {'kind': 'fixture', 'id': 'replacement'}
                        if path == changed else probe(path))
    with pytest.raises(InvalidMediaInventory, match='changed'):
        copy(source, inventory, target)
    assert (target / 'TREE-INCOMPLETE').exists()
    assert not (target / 'TREE-COMPLETE.json').exists()


def test_volume_change_before_completion_leaves_incomplete_tree(tmp_path, monkeypatch):
    source, inventory, target = fixture_tree(tmp_path)
    probe = media_tree.volume_identity
    def swap(event):
        if event['phase'] != 'copy':
            return
        monkeypatch.setattr(media_tree, 'volume_identity', lambda path: {'kind': 'fixture', 'id': 'new'}
                            if path == target else probe(path))
    with pytest.raises(InvalidMediaInventory, match='volume changed'):
        copy(source, inventory, target, progress=swap)
    assert (target / 'TREE-INCOMPLETE').exists()
    assert not (target / 'TREE-COMPLETE.json').exists()
