import hashlib
import os
import subprocess
from types import SimpleNamespace

import pytest

from app.video import avi, staging
from app.storage.disks import DiskError


def scratch(tmp_path):
    stage = tmp_path / 'data/conversion-temp/7'
    stage.mkdir(parents=True)
    for name in ('input.avi', 'input.avi.partial', 'output.mp4', 'previous-123.mp4',
                 'frame-validation.json', 'mts-plan.json', 'mts-plan.json.123.tmp'):
        (stage / name).write_bytes(b'scratch')
    return stage


def test_cleanup_is_bounded_and_repeatable(tmp_path):
    stage = scratch(tmp_path)
    other = stage.parent / '8'
    other.mkdir()
    (other / 'input.avi').write_bytes(b'other')
    staging.cleanup(tmp_path / 'data', 7, lambda: None)
    staging.cleanup(tmp_path / 'data', 7, lambda: None)
    assert not stage.exists()
    assert (other / 'input.avi').read_bytes() == b'other'


@pytest.mark.parametrize('problem', ['unknown', 'directory', 'lease', 'guard', 'id'])
def test_cleanup_fails_closed(tmp_path, problem):
    stage = scratch(tmp_path)
    if problem == 'unknown': (stage / 'important.txt').touch()
    if problem == 'directory': (stage / 'nested').mkdir()
    if problem == 'lease': (tmp_path / 'data/video-decoder-busy.json').touch()
    def guard():
        if problem == 'guard': raise DiskError('Disk gone')
    with pytest.raises((OSError, ValueError)):
        staging.cleanup(tmp_path / 'data', '../7' if problem == 'id' else 7, guard)
    assert (stage / 'input.avi').exists()


def test_cleanup_rejects_hardlinks(tmp_path):
    stage = scratch(tmp_path)
    outside = tmp_path / 'original'
    outside.write_bytes(b'original')
    (stage / 'output.mp4').unlink()
    (stage / 'output.mp4').hardlink_to(outside)
    with pytest.raises(OSError): staging.cleanup(tmp_path / 'data', 7, lambda: None)
    assert outside.read_bytes() == b'original'
    assert (stage / 'input.avi').exists()


def test_cleanup_rejects_redirected_stage(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'output.mp4').write_bytes(b'keep')
    base = tmp_path / 'data/conversion-temp'
    base.mkdir(parents=True)
    stage = base / '7'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(stage), str(outside)], check=True, capture_output=True)
    else:
        stage.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(OSError): staging.cleanup(tmp_path / 'data', 7, lambda: None)
        assert (outside / 'output.mp4').read_bytes() == b'keep'
    finally:
        if os.name == 'nt': stage.rmdir()
        else: stage.unlink()


@pytest.mark.parametrize('resumed', [False, True])
def test_finished_conversion_removes_all_scratch(tmp_path, monkeypatch, resumed):
    stage = scratch(tmp_path)
    monkeypatch.setattr(avi, 'DATA', tmp_path / 'data')
    source = tmp_path / 'source.avi'
    source.write_bytes(b'original')
    target = tmp_path / 'published.mp4'
    target.write_bytes(b'published')
    calls = []
    db = SimpleNamespace(execute=lambda *a: calls.append(a), commit=lambda: calls.append('commit'))
    ctx = SimpleNamespace(db=db, guard=lambda: None, source=source, io_mibps=0,
                          flv_handler=False, config={'avi_conversion': {'remove_destination_avi': False}})
    avi.finish_conversion(ctx, {'id': 7}, target, None if resumed else SimpleNamespace())
    assert not stage.exists()
    assert source.read_bytes() == b'original' and target.read_bytes() == b'published'
    assert calls[-1] == 'commit'


@pytest.mark.parametrize('failure', ['validation', 'disk', 'missing', 'changed', 'lease'])
def test_failed_conversion_cleanup_requires_verified_original(tmp_path, monkeypatch, failure):
    stage = scratch(tmp_path)
    monkeypatch.setattr(avi, 'DATA', tmp_path / 'data')
    original = tmp_path / 'source.avi'
    original.write_bytes(b'original')
    media = {'id': 7, 'relative_path': original.name, 'size': 8,
             'sha256': hashlib.sha256(b'original').hexdigest()}
    def fail(ctx):
        if failure == 'disk': raise DiskError('Disk gone')
        if failure == 'missing': original.unlink()
        if failure == 'changed': original.write_bytes(b'changed!')
        if failure == 'lease': (tmp_path / 'data/video-decoder-busy.json').touch()
        raise ValueError('Conversion frame count differs')
    monkeypatch.setattr(avi, 'publish_conversion', fail)
    with pytest.raises((ValueError, OSError)):
        avi.convert_one(None, media, {'destination': {'root': str(tmp_path)}}, {}, SimpleNamespace(check=lambda: None))
    assert stage.exists() == (failure != 'validation')
    if failure == 'validation': assert original.read_bytes() == b'original'
