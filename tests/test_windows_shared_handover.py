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
