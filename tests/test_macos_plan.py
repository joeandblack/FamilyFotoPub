import errno
from pathlib import Path

from installer.macos import plan


def test_existing_state_is_reported_without_reading_credentials(tmp_path, monkeypatch):
    config = tmp_path/'config/local.json'
    config.parent.mkdir(); config.write_text('not JSON: private credential sentinel')
    monkeypatch.setattr(plan, 'port_status', lambda port: 'available (not reserved)')
    before = {p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = plan.inspect(tmp_path, {'database':5433, 'web':4341}, brew_root=tmp_path/'missing-tools')
    assert any('Existing installation' in row for row in result['blockers'])
    assert any('Homebrew must be installed' in row for row in result['blockers'])
    assert 'sentinel' not in str(result)
    assert before == {p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_denied_socket_is_not_reported_as_available_or_occupied(monkeypatch):
    def denied(*args, **kwargs): raise PermissionError(errno.EPERM, 'denied')
    monkeypatch.setattr(plan.socket, 'socket', denied)
    assert plan.port_status(5433).startswith('unverified')


def test_occupied_socket_report(monkeypatch):
    def busy(*args, **kwargs): raise OSError(errno.EADDRINUSE, 'busy')
    monkeypatch.setattr(plan.socket, 'socket', busy)
    assert plan.port_status(5433) == 'occupied'


def test_overlapping_ports_and_external_data_reported(tmp_path, monkeypatch):
    monkeypatch.setenv('FAMILYFOTO_DATA_DIR', '/private-data')
    monkeypatch.setattr(plan, 'port_status', lambda port: 'available (not reserved)')
    result = plan.inspect(tmp_path, {'database':5433,'web':5433}, brew_root=tmp_path)
    assert 'Requested ports overlap' in result['blockers']
    assert any('override' in row for row in result['blockers'])
    assert list(tmp_path.iterdir()) == []
