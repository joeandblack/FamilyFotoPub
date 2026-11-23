import os
import subprocess

import pytest

from app.fastbackup.tests.windows_copy_fixture import _copy, _guid, run_synthetic
from app.fastbackup.windows_pinned_directory import PinnedDirectory, PinnedSourceFile
from app.fastbackup.tests import windows_copy_fixture as native


@pytest.mark.parametrize('size', [-1, True, 16 * 1024**2 + 1])
def test_cap(size):
    with pytest.raises(ValueError):
        run_synthetic(size)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows fixture')
@pytest.mark.parametrize('size', [0, 2048])
def test_real_fixture(size):
    assert run_synthetic(size)['verified']


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows fixture')
def test_reject_existing_and_skip_named_stream(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'source')
    target.write_bytes(b'keep')
    with PinnedDirectory(_guid(tmp_path)), PinnedSourceFile(_guid(source)):
        with pytest.raises(OSError):
            _copy(_guid(source), _guid(target))
        assert target.read_bytes() == b'keep'
    with open(str(source) + ':extra', 'wb') as stream:
        stream.write(b'not inventoried')
    with PinnedDirectory(_guid(tmp_path)), PinnedSourceFile(_guid(source)):
        _copy(_guid(source), _guid(tmp_path) + '\\new')
    assert (tmp_path / 'new').read_bytes() == b'source'
    with pytest.raises(FileNotFoundError):
        with open(str(tmp_path / 'new') + ':extra', 'rb'):
            pytest.fail('Alternate stream was copied')


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows fixture')
def test_late_unlisted_child_is_not_copied(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'admitted')
    with PinnedDirectory(_guid(tmp_path)), PinnedSourceFile(_guid(source)):
        (tmp_path / 'unlisted').write_bytes(b'not admitted')
        _copy(_guid(source), _guid(tmp_path) + '\\target')
    assert (tmp_path / 'target').read_bytes() == b'admitted'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['source', 'target', 'unlisted']


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows fixture')
def test_dangling_target_link_not_overwritten(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'admitted')
    try:
        target.symlink_to(tmp_path / 'missing')
    except OSError as exc:
        if exc.winerror == 1314:
            pytest.skip('Windows symlink privilege unavailable')
        raise
    with PinnedDirectory(_guid(tmp_path)), PinnedSourceFile(_guid(source)):
        with pytest.raises(OSError):
            _copy(_guid(source), _guid(tmp_path) + '\\target')
    assert target.is_symlink()
    assert not (tmp_path / 'missing').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows fixture parent')
def test_outer_timeout_is_bounded(monkeypatch):
    def timeout(command, **kwargs):
        assert kwargs['timeout'] == 30
        raise subprocess.TimeoutExpired(command, 30)
    monkeypatch.setattr(native.subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        run_synthetic(1)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows fixture')
def test_worker_rejects_content_mismatch(tmp_path, monkeypatch):
    def corrupt(source, target):
        from pathlib import Path
        Path(target).write_bytes(b'wrong')
    monkeypatch.setattr(native, '_copy', corrupt)
    with pytest.raises(ValueError, match='Content mismatch'):
        native._worker(tmp_path.resolve(), 5)
