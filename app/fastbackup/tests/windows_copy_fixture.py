"""Bounded disposable worker for production CopyFile2 regression tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from app.fastbackup.native_copy import guid_path as _guid
from app.fastbackup.windows_bulk_copy import copy_file as _copy
from app.fastbackup.windows_pinned_directory import PinnedDirectory, PinnedSourceFile


def _worker(root, size):
    root = Path(root)
    if root != root.resolve() or not root.is_dir() or any(root.iterdir()):
        raise ValueError('Expected canonical empty owned fixture')
    if type(size) is not int or not 0 <= size <= 16 * 1024**2:
        raise ValueError('Fixture exceeds cap')
    source, target = root / 'source.bin', root / 'target.bin'
    source.write_bytes(b'0123456789abcdef' * (size // 16) + b'x' * (size % 16))
    with PinnedDirectory(_guid(root)), PinnedSourceFile(_guid(source)) as pinned:
        _copy(_guid(source), _guid(root) + '\\target.bin')
        pinned.recheck()
        if target.read_bytes() != source.read_bytes() or target.stat().st_size != size:
            raise ValueError('Content mismatch')
        with target.open('r+b') as stream:
            os.fsync(stream.fileno())
    return {'bytes': size, 'verified': True}


def run_synthetic(size=2048):
    if type(size) is not int or not 0 <= size <= 16 * 1024**2:
        raise ValueError('Fixture exceeds cap')
    if os.name != 'nt':
        raise RuntimeError('Windows required')
    with tempfile.TemporaryDirectory(prefix='familyfoto-native-file-') as temporary:
        command = [sys.executable, '-B', '-c',
                   'import json,sys; from app.fastbackup.tests.windows_copy_fixture import _worker; '
                   'print(json.dumps(_worker(sys.argv[1],int(sys.argv[2]))))',
                   str(Path(temporary).resolve()), str(size)]
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[3],
                                capture_output=True, text=True, timeout=30, check=True,
                                stdin=subprocess.DEVNULL)
        return json.loads(result.stdout)
