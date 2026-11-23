"""Wire-format and ordering contracts shared by Standard and Fast capture."""
import json

from cryptography.fernet import Fernet
import pytest

from app.backup import library


@pytest.mark.parametrize('engine', [None, 'fastbackup-v1'])
def test_capture_job_preserves_sealed_record(tmp_path, monkeypatch, engine):
    key = Fernet.generate_key()
    binding = {'format': library.JOB_FORMAT, 'roots': {'media': {'path': 'source'}}, 'retire_to': None}
    saved = []
    monkeypatch.setattr(library, 'save_private', lambda path, value: saved.append((path, value)))
    library.write_capture_job(tmp_path, key, binding, 'capture-id', 'library-id', engine=engine)
    path, record = saved.pop()
    assert path == tmp_path / library.JOB_RECORD
    assert set(record) == {'sealed'}
    expected = {**binding, 'id': 'capture-id', 'library_id': 'library-id'}
    if engine is not None:
        expected['engine'] = engine
    assert json.loads(Fernet(key).decrypt(record['sealed'].encode())) == expected
    assert 'id' not in binding


@pytest.mark.parametrize('verified', [True, False])
@pytest.mark.parametrize('thumbnails', [None, 'thumbnail-reference'])
@pytest.mark.parametrize('retire_to', [None, 'successor-id'])
def test_library_index_preserves_payload_and_publication_order(
        tmp_path, monkeypatch, verified, thumbnails, retire_to):
    class Key:
        def binding(self):
            return {'context': 'password-context'}

    media = {'inventory_sha256': 'a' * 64, 'files': 2, 'bytes': 100, 'content_verified': verified}
    counts = {'catalog_media': 2, 'retained_originals': 0, 'removed_originals': 0}
    events = []
    monkeypatch.setattr(library, 'reference', lambda value: {'reference': value})
    monkeypatch.setattr(library, 'write_handover', lambda path: events.append(('handover', path)))
    def seal(stream, path, key, **kwargs):
        events.append(('seal', stream.read(), path, key, kwargs))
    monkeypatch.setattr(library, 'seal_stream', seal)
    key, guard = Key(), object()
    payload = library.publish_library_index(tmp_path, key, 'capture-id', 'library-id', 'catalog-reference',
                                           media, counts, thumbnails, guard_factory=guard, retire_to=retire_to)
    expected = {'format': 'familyfoto-library-plain-media-v2', 'id': 'capture-id', 'library_id': 'library-id',
                'catalog': {'reference': 'catalog-reference'}, 'media_inventory_sha256': 'a' * 64,
                'media_files': 2, 'media_bytes': 100, 'reconciliation': counts, 'media_encrypted': False,
                'password': {'context': 'password-context'}, 'capture_mode': 'offline', 'activation_allowed': False}
    if thumbnails is not None:
        expected['thumbnails'] = {'reference': thumbnails}
    if retire_to is not None:
        expected['migration_target'] = retire_to
    if not verified:
        expected['media_read_back'] = False
    assert payload == expected
    raw = json.dumps(expected, sort_keys=True).encode()
    assert events == [('handover', tmp_path), ('seal', raw, tmp_path / 'library', key,
                      {'estimated_bytes': len(raw), 'metadata': {'kind': expected['format']}, 'guard_factory': guard})]


def test_failed_handover_does_not_publish_index(tmp_path, monkeypatch):
    def fail(path):
        raise OSError('handover failed')
    monkeypatch.setattr(library, 'reference', lambda value: value)
    monkeypatch.setattr(library, 'write_handover', fail)
    monkeypatch.setattr(library, 'seal_stream', lambda *args, **kwargs: pytest.fail('index must not be published'))
    class Key:
        def binding(self):
            return {}
    with pytest.raises(OSError, match='handover failed'):
        library.publish_library_index(tmp_path, Key(), 'id', 'library', {},
            {'inventory_sha256': 'a' * 64, 'files': 0, 'bytes': 0, 'content_verified': True}, {}, None)
