import json

import pytest

from app.backup import windows_handover
from installer.common.manifest import validate_release_link


URL = 'https://github.com/joeandblack/FamilyFotoPub/releases/download/test-1/FamilyFotoSetup.exe'


def receipt(path, url=URL):
    path.write_text(json.dumps({'windows_handover': {'release_id': 'test-1', 'setup_url': url}}))
    return path


def test_legacy_install_has_no_new_handover(tmp_path):
    assert not windows_handover.write_windows_handover(tmp_path, tmp_path / 'absent')
    assert not (tmp_path / windows_handover.FOLDER).exists()


def test_release_pinned_instructions_and_retry(tmp_path):
    metadata = receipt(tmp_path / 'receipt.json')
    assert windows_handover.write_windows_handover(tmp_path, metadata)
    target = tmp_path / windows_handover.FOLDER / 'README.txt'
    first = target.read_bytes()
    assert URL.encode() in first and b'--backup' in first and b'-Mode Restore' in first
    assert b'git clone' not in first
    windows_handover.write_windows_handover(tmp_path, metadata)
    assert target.read_bytes() == first


@pytest.mark.parametrize('url', ['https://evil.example/setup.exe',
                                'https://github.com/joeandblack/FamilyFotoPub/releases/latest/download/FamilyFotoSetup.exe',
                                URL + '?redirect=1', URL.replace('test-1', 'other')])
def test_reject_unpinned_or_foreign_url_before_writes(tmp_path, url, caplog):
    metadata = receipt(tmp_path / 'receipt.json', url)
    assert not windows_handover.write_windows_handover(tmp_path, metadata)
    assert 'invalid release receipt' in caplog.text
    assert not (tmp_path / windows_handover.FOLDER).exists()


def test_link_validation():
    assert validate_release_link('test-1', URL) == URL
    with pytest.raises(ValueError):
        validate_release_link('../test-1', URL)
    assert validate_release_link('test-1', URL.replace('.exe', '.dmg'), 'macos-arm64')
    with pytest.raises(ValueError):
        validate_release_link('test-1', URL, 'macos-arm64')


@pytest.mark.parametrize('content', ['{', '{}', '[]', '{"windows_handover":{}}'])
def test_damaged_receipt_is_advisory_and_removes_stale(tmp_path, content):
    metadata = receipt(tmp_path / 'receipt.json')
    windows_handover.write_windows_handover(tmp_path, metadata)
    target = tmp_path / windows_handover.FOLDER / 'README.txt'
    assert target.exists()
    metadata.write_text(content)
    assert not windows_handover.write_windows_handover(tmp_path, metadata)
    assert not target.exists()


def test_handover_names_reserved_from_library():
    from app.backup import library
    assert windows_handover.FOLDER in library.CAPTURE_ENTRIES


@pytest.mark.parametrize('commit', ['a' * 40, ''])
def test_source_backup_writes_pinned_windows_launcher(tmp_path, monkeypatch, commit):
    from app.backup import handover
    monkeypatch.setattr(handover, 'source_commit', lambda: commit)
    handover.write_handover(tmp_path, tmp_path / 'absent')
    folder = tmp_path / windows_handover.FOLDER
    text = (folder / windows_handover.LAUNCHER).read_text()
    assert text.count(f"$commit = '{commit}'") == 1
    assert 'release/0.1' not in text
    assert text.index('Assert-RestoreCommit $commit') < text.index('$identity =')
    assert (commit or 'unidentified') in (folder / 'README.txt').read_text()


def test_release_retry_removes_source_launcher(tmp_path):
    windows_handover.write_source_handover(tmp_path, 'b' * 40)
    assert windows_handover.write_windows_handover(tmp_path, receipt(tmp_path / 'receipt'))
    folder = tmp_path / windows_handover.FOLDER
    assert not (folder / windows_handover.LAUNCHER).exists()
    assert URL in (folder / 'README.txt').read_text()


def test_source_retry_replaces_release_instructions(tmp_path, monkeypatch):
    from app.backup import handover
    metadata = receipt(tmp_path / 'receipt')
    windows_handover.write_windows_handover(tmp_path, metadata)
    monkeypatch.setattr(handover, 'source_commit', lambda: 'c' * 40)
    handover.write_handover(tmp_path, tmp_path / 'absent')
    assert URL not in (tmp_path / windows_handover.FOLDER / 'README.txt').read_text()


def test_source_launcher_refuses_linked_output(tmp_path):
    import os
    folder = tmp_path / windows_handover.FOLDER
    folder.mkdir()
    other = tmp_path / 'untouched'
    other.write_text('keep')
    os.link(other, folder / windows_handover.LAUNCHER)
    with pytest.raises(ValueError):
        windows_handover.write_source_handover(tmp_path, 'a' * 40)
    assert other.read_text() == 'keep'


@pytest.mark.parametrize('mac_release,windows_release', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('commit', ['a' * 40, ''])
def test_source_identity_resolved_only_once_when_needed(tmp_path, monkeypatch, mac_release, windows_release, commit):
    from app.backup import handover
    calls = []
    def identify():
        calls.append(True)
        return commit
    monkeypatch.setattr(handover, 'source_commit', identify)
    doc = {}
    if mac_release:
        doc['mac_handover'] = {'release_id': 'test-1', 'setup_url': URL.replace('.exe', '.dmg')}
    if windows_release:
        doc['windows_handover'] = {'release_id': 'test-1', 'setup_url': URL}
    metadata = tmp_path / 'receipt.json'
    metadata.write_text(json.dumps(doc))
    handover.write_handover(tmp_path, metadata)
    assert len(calls) == (0 if mac_release and windows_release else 1)
    if not mac_release:
        assert f"commit='{commit}'" in (tmp_path / handover.FOLDER / handover.LAUNCHER).read_text()
    if not windows_release:
        assert f"$commit = '{commit}'" in (tmp_path / windows_handover.FOLDER / windows_handover.LAUNCHER).read_text()
