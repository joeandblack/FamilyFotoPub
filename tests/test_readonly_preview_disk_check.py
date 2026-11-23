"""The read-only preview blocks child processes, but macOS needs one to identify the media disk."""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from app.service.readonly_preview import disk_check_commands

ROOT = Path(__file__).resolve().parents[1]


def guarded(tmp_path, body):
    """Run `body` in a fresh interpreter, because an audit hook can't be removed."""
    code = textwrap.dedent('''
        import sys
        from pathlib import Path
        from app.migration.disk import disk_binding
        from app.service.readonly_preview import disk_check_commands, install_write_guard
        scratch = Path(sys.argv[1])
        cfg = {'destination': disk_binding(scratch)}
        install_write_guard(scratch, allowed_subprocesses=disk_check_commands(cfg))
    ''') + textwrap.dedent(body)
    return subprocess.run([sys.executable, '-c', code, str(tmp_path)], cwd=ROOT, capture_output=True, text=True, timeout=60)


def test_only_macos_needs_a_command_for_the_disk_check(monkeypatch):
    cfg = {'destination': {'drive': '/Volumes/Photos'}}
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert disk_check_commands(cfg) == ()
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert disk_check_commands(cfg) == [('/usr/sbin/diskutil', 'info', '-plist', '/Volumes/Photos')]
    assert disk_check_commands({'destination': {'root': '/Volumes/Photos'}}) == ()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS identifies a disk with diskutil')
def test_the_disk_check_runs_in_the_preview_and_nothing_else_does(tmp_path):
    result = guarded(tmp_path, '''
        from app.storage.disks import volume_serial
        assert volume_serial(cfg['destination']['drive']) == cfg['destination']['volume_serial']
        import subprocess
        for command in (['/usr/sbin/diskutil', 'list'], ['/usr/sbin/diskutil', 'info', '-plist', '/'], ['/bin/ls']):
            try:
                subprocess.run(command, capture_output=True)
            except PermissionError:
                continue
            raise SystemExit('allowed ' + ' '.join(command))
    ''')
    assert result.returncode == 0, result.stderr
