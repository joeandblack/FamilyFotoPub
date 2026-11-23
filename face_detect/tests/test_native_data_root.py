import os
from pathlib import Path
import subprocess
import sys

import pytest

from face_detect.jobs import automatic_people_local


def test_native_worker_uses_configured_data_root(tmp_path):
    data = tmp_path / 'external data'
    code = (
        'from face_detect.jobs.automatic_people_local import PRIVATE; '
        'print(PRIVATE.resolve())'
    )
    result = subprocess.run(
        [sys.executable, '-c', code],
        env={**os.environ, 'FAMILYFOTO_DATA_DIR': str(data)},
        capture_output=True, text=True, check=True, timeout=30,
    )
    assert Path(result.stdout.strip()) == (data / 'face_detect/profiles').resolve()


def test_native_worker_rejects_unrelated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(automatic_people_local, 'PRIVATE', tmp_path / 'profiles')
    with pytest.raises(ValueError, match='Private evaluation root required'):
        automatic_people_local.runtime(tmp_path / 'unrelated')
