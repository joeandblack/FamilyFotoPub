"""Resuming a restore after its disk remounts: same disk only, and nothing is trusted early."""
import json
import sys
from types import SimpleNamespace

import pytest

from app.backup import encrypted_library, library_restore, recovery, restic_restore_job
from app.backup.encrypted import InvalidArtifact
from app.backup.postgres import Endpoint
from app.backup.restic import ResticFailed
from app.backup.space import directory_identity
from app.backup.volumes import same_except_device, same_job
from app.backup.passwords import BackupPassword
from tests.backup_keys import backup_key
from tests.test_backup_capture_retry import ample, attach, detach, Interrupted, new_image


KEY = backup_key()
ADMIN = Endpoint('127.0.0.1', 55489, 'postgres', 'fixture')
FINISHED = {'resumed': True}


class Job:
    """One restore job type, interrupted once and then resumable."""

    def __init__(self, target, record, identity, module, refusal, start):
        self.target, self.record, self.identity = target, target / record, identity
        self.module, self.refusal, self.start = module, refusal, start
        self.attempts = []

    def work(self, *args, **kwargs):
        self.attempts.append('entered')
        if len(self.attempts) == 1:
            raise Interrupted('restore')
        return FINISHED

    def edit(self, change):
        value = json.loads(self.record.read_text())
        change(value)
        self.record.write_text(json.dumps(value))


def snapshot_job(root, patch):
    target = root / 'job'
    job = Job(target, 'job.json', 'directory', restic_restore_job, ResticFailed, None)
    engine = SimpleNamespace(repository=root / 'repo', password_file=root / 'key', check=lambda repository: None,
                             snapshot=lambda snapshot: {'restore_root': '/source'},
                             run=lambda *args, **kwargs: job.work())

    def start(resume):
        restic_restore_job.restore_snapshot_job(engine, 'a' * 64, 'b' * 64, target, required_bytes=1024,
                                                resume=resume, guard_factory=ample)
        return FINISHED
    job.start = start
    return job


def catalog_job(root, patch):
    target = root / 'catalog-target'
    job = Job(target, 'catalog-job.json', 'identity', recovery, ValueError, None)
    patch.setattr(recovery, 'verify_catalog', lambda *args: {'id': 'capture'})
    patch.setattr(recovery, 'verify_artifact', lambda *args: {'id': 'state', 'sha256': 'x', 'metadata': {'file_bytes': 5}})
    patch.setattr(recovery, '_restore_catalog_attempt', job.work)

    def start(resume):
        return recovery.restore_catalog(root / 'backup', KEY, target, ADMIN, None, expected_cluster_directory=root,
                                        database_budget=100, resume=resume, guard_factory=ample)
    job.start = start
    return job


def library_job(root, patch):
    target = root / 'library-target'
    job = Job(target, 'restore-job.json', 'identity', library_restore, InvalidArtifact, None)
    patch.setattr(library_restore, 'verify_library', lambda *args, **kwargs: {
        'id': 'capture', 'media_files': 1, 'media_bytes': 100, 'media_inventory_sha256': 'digest'})
    patch.setattr(library_restore, 'verify_artifact', lambda *args: {'metadata': {'file_bytes': 5}})
    patch.setattr(library_restore, '_restore_contents', job.work)

    def start(resume):
        return library_restore.restore_library(root / 'backup', KEY, target, ADMIN, None, expected_cluster_directory=root,
                                               database_budget=100, resume=resume, guard_factory=ample)
    job.start = start
    return job


def encrypted_job(root, patch):
    target = root / 'encrypted-target'
    job = Job(target, 'restore-job.json', 'target_identity', encrypted_library, InvalidArtifact, None)
    engine = SimpleNamespace(repository=root / 'repository', check=lambda repository: None)
    patch.setattr(encrypted_library, '_restore_encrypted_contents', job.work)

    def start(resume):
        return encrypted_library.restore_encrypted_library(
            engine, 'a' * 64, 'b' * 64, BackupPassword('fixture password'), target, ADMIN, None, expected_cluster_directory=root,
            database_budget=100, metadata_budget=100, resume=resume, guard_factory=ample)
    job.start = start
    return job


JOBS = [snapshot_job, catalog_job, library_job, encrypted_job]
each_job = pytest.mark.parametrize('make', JOBS, ids=lambda make: make.__name__)


def interrupted(make, root, patch):
    job = make(root, patch)
    with pytest.raises(Interrupted):
        job.start(False)
    return job


def shift(job):
    job.edit(lambda value: value[job.identity].__setitem__(0, value[job.identity][0] + 7))


@each_job
def test_resume_accepts_a_remount_of_the_same_disk(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    shift(job)
    assert job.start(True) == FINISHED
    assert job.attempts == ['entered', 'entered']


@each_job
def test_resume_without_a_remount_still_works(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    assert job.start(True) == FINISHED


@each_job
def test_a_replaced_target_is_refused_after_a_remount(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    job.edit(lambda value: value[job.identity].__setitem__(1, value[job.identity][1] + 1))
    shift(job)
    with pytest.raises(job.refusal, match='identity'):
        job.start(True)
    assert job.attempts == ['entered']


@each_job
def test_another_disk_is_refused_even_at_the_same_device_and_inode(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    monkeypatch.setattr(job.module, 'volume_identity', lambda path: {'kind': 'test', 'id': 'other-disk'})
    with pytest.raises(job.refusal, match='identity'):
        job.start(True)
    assert job.attempts == ['entered']


@each_job
def test_another_disk_is_refused_after_a_remount(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    shift(job)
    monkeypatch.setattr(job.module, 'volume_identity', lambda path: {'kind': 'test', 'id': 'other-disk'})
    with pytest.raises(job.refusal, match='identity'):
        job.start(True)
    assert job.attempts == ['entered']


@each_job
def test_a_remount_does_not_excuse_any_other_field(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    shift(job)
    job.edit(lambda value: value.update(format='another-format'))
    with pytest.raises(job.refusal, match='identity'):
        job.start(True)
    assert job.attempts == ['entered']


@each_job
@pytest.mark.parametrize('value', [None, 'changed', [1], [1, 2, 3]], ids=['missing', 'text', 'short', 'long'])
def test_a_badly_shaped_identity_is_refused(tmp_path, monkeypatch, make, value):
    job = interrupted(make, tmp_path, monkeypatch)
    job.edit(lambda record: record.__setitem__(job.identity, value))
    with pytest.raises(job.refusal, match='identity'):
        job.start(True)
    assert job.attempts == ['entered']


@pytest.mark.parametrize('make', [snapshot_job, catalog_job, encrypted_job], ids=lambda make: make.__name__)
def test_an_exact_record_stays_exact(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    job.edit(lambda value: value.update(unexpected=True))
    with pytest.raises(job.refusal, match='identity'):
        job.start(True)
    assert job.attempts == ['entered']


@each_job
def test_a_resume_needs_the_job_record(tmp_path, monkeypatch, make):
    job = interrupted(make, tmp_path, monkeypatch)
    job.record.write_text(json.dumps([]))
    with pytest.raises(job.refusal, match='identity'):
        job.start(True)
    assert job.attempts == ['entered']


def test_same_job_only_forgives_the_named_device():
    expected = {'format': 'f', 'identity': [1, 2], 'other': [3, 4], 'volume': 'v'}
    assert same_job({**expected, 'identity': [9, 2]}, expected, 'identity')
    assert not same_job({**expected, 'identity': [9, 8]}, expected, 'identity')
    assert not same_job({**expected, 'other': [9, 4]}, expected, 'identity')
    assert not same_job({**expected, 'volume': 'w'}, expected, 'identity')
    assert not same_job({**expected, 'extra': 1}, expected, 'identity')
    assert not same_job(['identity'], expected, 'identity')
    assert same_except_device([9, 2], (1, 2))
    assert not same_except_device('9', (1, 2))


@pytest.mark.skipif(sys.platform != 'darwin', reason='Detaches and reattaches real APFS disk images')
@each_job
@pytest.mark.ci_apfs
def test_real_apfs_remount_resumes_and_a_replacement_disk_is_refused(tmp_path, monkeypatch, make):
    image, filler, other = (tmp_path / f'{name}.dmg' for name in ('target', 'filler', 'other'))
    for path in (image, filler, other):
        new_image(path, path.stem)
    mount, filler_mount = tmp_path / 'mnt-target', tmp_path / 'mnt-filler'
    attached = []

    def attach_at(path, where):
        attach(path, where)
        attached.append(where)

    def detach_from(where):
        detach(where)
        attached.remove(where)
    try:
        attach_at(image, mount)
        job = interrupted(make, mount, monkeypatch)
        before = directory_identity(job.target)
        detach_from(mount)
        # Another disk takes the old device number, as it would after a real reconnection.
        attach_at(filler, filler_mount)
        attach_at(image, mount)
        assert directory_identity(job.target)[0] != before[0]
        assert directory_identity(job.target)[1] == before[1]
        assert job.start(True) == FINISHED

        # A different disk at the same path, holding a copy of the job, is a replacement.
        record = job.record.read_bytes()
        # The copy carries the backup's public password context too, as a real copied folder would.
        context = (job.target / 'password.json')
        context = context.read_bytes() if context.is_file() else None
        marker = {path.name for path in job.target.iterdir() if path.is_file() and path.stat().st_size == 0}
        detach_from(mount)
        attach_at(other, mount)
        job.target.mkdir()
        job.record.write_bytes(record)
        if context is not None:
            (job.target / 'password.json').write_bytes(context)
        for name in marker:
            (job.target / name).touch()
        with pytest.raises(job.refusal, match='identity'):
            job.start(True)
    finally:
        for where in reversed(attached):
            detach(where)
