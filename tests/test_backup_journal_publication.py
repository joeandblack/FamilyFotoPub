import json

import pytest

from app.backup import recovery


def test_abandoned_publications_never_block_or_replace_committed_state(tmp_path):
    journal = tmp_path / 'journal.json'
    abandoned = tmp_path / 'journal.json.part'
    abandoned.write_bytes(b'{incomplete')
    recovery.save_private(journal, {'state': 'planned'})
    recovery.save_private(journal, {'state': 'verified'})
    assert json.loads(journal.read_text()) == {'state': 'verified'}
    assert abandoned.read_bytes() == b'{incomplete'


def test_failed_publication_preserves_old_journal_and_can_retry(tmp_path, monkeypatch):
    journal = tmp_path / 'journal.json'
    recovery.save_private(journal, {'state': 'planned'})
    replace = recovery.os.replace

    def disconnected(*args):
        raise OSError('Destination disconnected')

    monkeypatch.setattr(recovery.os, 'replace', disconnected)
    with pytest.raises(OSError, match='disconnected'):
        recovery.save_private(journal, {'state': 'created'})
    assert json.loads(journal.read_text()) == {'state': 'planned'}
    attempts = list(tmp_path.glob('journal.json.*.part'))
    assert len(attempts) == 1
    contents = attempts[0].read_bytes()
    monkeypatch.setattr(recovery.os, 'replace', replace)
    recovery.save_private(journal, {'state': 'verified'})
    assert json.loads(journal.read_text()) == {'state': 'verified'}
    assert attempts[0].read_bytes() == contents


def test_temporary_recognition_rejects_hardlinks_and_oversized_files(tmp_path):
    path = tmp_path / ('journal.json.' + 'a' * 32 + '.part')
    path.write_bytes(b'{incomplete')
    assert recovery.abandoned_publication(path, ('journal.json',))
    outside = tmp_path / 'outside'
    outside.hardlink_to(path)
    assert not recovery.abandoned_publication(path, ('journal.json',))
    outside.unlink()
    path.write_bytes(b'x' * 65537)
    assert not recovery.abandoned_publication(path, ('journal.json',))
