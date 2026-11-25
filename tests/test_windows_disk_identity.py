"""Windows disks without a hardware serial still get a complete identity, and setup's elevated step runs
under Windows 11's default execution policy."""
import os
from pathlib import Path
import subprocess

import pytest

from app.storage import disks

ROOT = Path(__file__).resolve().parents[1]
windows = pytest.mark.skipif(os.name != 'nt', reason='Runs Windows PowerShell')


def serial_for(disk, partition):
    """Run the snapshot's serial rule against stand-in Get-Disk and Get-Partition objects."""
    def literal(values):
        return '[PSCustomObject]@{' + ';'.join(f"{name}={value}" for name, value in values.items()) + '}'
    script = f'$disk={literal(disk)}; $p={literal(partition)}\n{disks.DISK_SERIAL}\n$serial'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@windows
def test_a_reported_serial_is_kept_exactly():
    assert serial_for({'SerialNumber': "'  2535K2A00CE4    '", 'Signature': 0},
                      {'Guid': "'{B3040E03-9191-49AB-9D32-6579CDC4AAF9}'", 'Offset': 16777216}) == '2535K2A00CE4'


@windows
def test_a_gpt_disk_without_serial_is_named_by_its_partition():
    """Hyper-V's "Msft Virtual Disk" reports an empty SerialNumber."""
    assert serial_for({'SerialNumber': "''", 'Signature': 0},
                      {'Guid': "'{B3040E03-9191-49AB-9D32-6579CDC4AAF9}'", 'Offset': 16777216}) \
        == 'partition:b3040e03-9191-49ab-9d32-6579cdc4aaf9'


@windows
def test_an_mbr_disk_without_serial_is_named_by_signature_and_offset():
    assert serial_for({'SerialNumber': '$null', 'Signature': 305419896}, {'Guid': '$null', 'Offset': 1048576}) \
        == 'mbr:12345678:1048576'


@windows
def test_nothing_to_name_stays_empty_and_is_refused_later():
    assert serial_for({'SerialNumber': "''", 'Signature': 0}, {'Guid': "''", 'Offset': 1048576}) == ''


@windows
def test_this_pcs_system_disk_still_reports_its_own_serial():
    """The real query: a disk with a serial keeps the value recorded before the fallback existed."""
    drive = os.environ.get('SystemDrive', 'C:')[0]
    expected = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                               f"([string](Get-Partition -DriveLetter {drive} | Get-Disk).SerialNumber).Trim()"],
                              capture_output=True, text=True, timeout=60).stdout.strip()
    observed = disks.snapshot([drive])[drive]
    assert observed['serial'] == (expected or observed['serial'])
    assert all(observed[name] for name in ('serial', 'volume_serial', 'volume_id'))


def identity(serial):
    return {'drive': 'E', 'serial': serial, 'volume_serial': 'ABCD1234', 'volume_id': 'id'}


def test_an_empty_recorded_serial_still_matches_its_disk():
    """A library recorded while this disk's serial read as empty is not suddenly a different disk."""
    disks.match_disk(identity(''), identity('partition:b3040e03-9191-49ab-9d32-6579cdc4aaf9'))
    disks.match_disk(identity(''), identity('mbr:12345678:1048576'))


def test_the_fallback_never_stands_in_for_a_recorded_serial():
    with pytest.raises(disks.DiskError, match='serial'):
        disks.match_disk(identity('2535K2A00CE4'), identity('partition:b3040e03-9191-49ab-9d32-6579cdc4aaf9'))
    with pytest.raises(disks.DiskError, match='serial'):
        disks.match_disk(identity(''), identity('2535K2A00CE4'))
    with pytest.raises(disks.DiskError, match='volume_serial'):
        disks.match_disk(identity(''), {**identity('partition:x'), 'volume_serial': 'OTHER'})


def test_the_elevated_setup_step_bypasses_the_execution_policy():
    """Start-Process -Verb RunAs passes no environment, so the parent's Bypass does not reach the child,
    and a fresh Windows 11 (Restricted) refuses the script with no visible reason."""
    script = (ROOT / 'installer/windows/provision-prepared.ps1').read_text(encoding='utf-8')
    elevated = next(line for line in script.splitlines() if line.strip().startswith('$arguments ='))
    assert '-ExecutionPolicy Bypass -File' in elevated
    setup = (ROOT / 'installer/windows/online_setup.py').read_text(encoding='utf-8')
    assert "'-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',\n" in setup


def test_the_restore_readme_resume_command_runs_under_the_default_policy():
    from app.backup import windows_handover
    source = Path(windows_handover.__file__).read_text(encoding='utf-8')
    assert 'powershell.exe -ExecutionPolicy Bypass -File installer\\\\windows\\\\install.ps1 -Mode Restore' in source
