import json

import pytest

from app.backup import thumbnails as module
from app.backup.catalog import reference
from app.backup.diagnostics import ExplainedFailure
from app.backup.encrypted import InvalidArtifact
from tests.test_backup_media_tree import Guard
from tests.backup_keys import backup_key


KEY = backup_key()
FILES = {'thumbnails/ab/cd/abcd.jpg': b'photo thumbnail', 'thumbnails/ef/poster.webp': b'video poster' * 200_000,
         'previews/ab/abcd.jpg': b'heif preview', 'people/thumbnails/12.jpg': b'face'}


def populate(data, files=FILES):
    for name, content in files.items():
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


@pytest.fixture
def sealed(tmp_path):
    data = tmp_path / 'data'
    populate(data)
    # Not thumbnails: a log, a face profile's legacy cache, a half-written file, a link.
    (data / 'logs').mkdir()
    (data / 'logs/app.log').write_text('log')
    (data / 'thumbnails/ab/notes.txt').write_text('stray')
    (data / ('people/thumbnails/.123.' + 'a' * 32 + '.tmp')).write_bytes(b'partial')
    try:
        (data / 'thumbnails/link.jpg').symlink_to(data / 'logs/app.log')
    except OSError:
        pass  # Creating symlinks requires privilege on some Windows hosts; the rest still applies.
    backup = tmp_path / 'backup'
    backup.mkdir()
    manifest = module.capture_thumbnails(data, backup / module.FOLDER, KEY, guard_factory=Guard)
    return backup, manifest, tmp_path


def recovery(tmp_path, monkeypatch, backup, manifest, *, state='activated', listed=True):
    target = tmp_path / 'recovered'
    (target / 'catalog/files/config').mkdir(parents=True)
    (target / 'catalog/files/data').mkdir()
    (target / 'catalog/files/config/local.json').write_text(json.dumps(
        {'recovery': {'state': state, 'activation_required': state != 'activated'}}))
    payload = {'thumbnails': reference(manifest)} if listed else {}
    monkeypatch.setattr('app.backup.library.verify_library_metadata', lambda directory, key, **kwargs: payload)
    return target, target / 'catalog/files/data'


def restore(backup, target, **kwargs):
    return module.restore_thumbnails(backup, KEY, target, guard_factory=Guard, **kwargs)


def test_only_cached_images_are_sealed_and_nothing_is_readable_without_the_key(sealed):
    backup, manifest, _ = sealed
    assert manifest['metadata'] == {'kind': module.KIND, 'file_count': 4,
                                    'file_bytes': sum(len(content) for content in FILES.values())}
    raw = b''.join(path.read_bytes() for path in (backup / module.FOLDER).iterdir())
    assert b'photo thumbnail' not in raw and b'abcd.jpg' not in raw
    names = []
    module.walk_thumbnails(backup / module.FOLDER, KEY, manifest, lambda name, size, read: names.append(name))
    assert names == sorted(FILES)


def test_a_library_without_thumbnails_makes_no_copy(tmp_path):
    (tmp_path / 'data').mkdir()
    assert module.capture_thumbnails(tmp_path / 'data', tmp_path / 'out', KEY, guard_factory=Guard) is None
    assert not (tmp_path / 'out').exists()


def test_restore_puts_every_file_back_and_a_second_run_keeps_them(sealed, monkeypatch):
    backup, manifest, tmp_path = sealed
    target, data = recovery(tmp_path, monkeypatch, backup, manifest)
    result = restore(backup, target)
    assert (result['restored'], result['kept'], result['files']) == (4, 0, 4)
    for name, content in FILES.items():
        assert (data / name).read_bytes() == content
    assert not list(data.rglob('*.restoring'))
    again = restore(backup, target)
    assert (again['restored'], again['kept']) == (0, 4)


def test_restore_keeps_a_newer_thumbnail_and_replaces_a_cut_short_one(sealed, monkeypatch):
    backup, manifest, tmp_path = sealed
    target, data = recovery(tmp_path, monkeypatch, backup, manifest)
    populate(data, {'thumbnails/ab/cd/abcd.jpg': b'PHOTO THUMBNAIL', 'people/thumbnails/12.jpg': b'fa',
                    'thumbnails/zz/only-here.jpg': b'made after the backup'})
    leftover = data / ('thumbnails/ab/cd/abcd.jpg.' + 'b' * 32 + '.restoring')
    leftover.write_bytes(b'half')
    result = restore(backup, target)
    assert (result['restored'], result['kept'], result['in_use']) == (3, 1, 0)
    assert (data / 'thumbnails/ab/cd/abcd.jpg').read_bytes() == b'PHOTO THUMBNAIL'
    assert (data / 'people/thumbnails/12.jpg').read_bytes() == b'face'
    assert (data / 'thumbnails/zz/only-here.jpg').read_bytes() == b'made after the backup'
    assert not leftover.exists()


def test_a_thumbnail_the_server_has_open_is_left_and_the_rest_still_restore(sealed, monkeypatch):
    """Windows refuses to replace an open file. One such file must not stop the run."""
    backup, manifest, tmp_path = sealed
    target, data = recovery(tmp_path, monkeypatch, backup, manifest)
    populate(data, {'people/thumbnails/12.jpg': b'fa'})
    real = module.os.replace

    def replace(source, destination):
        if str(destination).endswith('12.jpg'):
            raise PermissionError(13, 'The process cannot access the file because it is being used')
        return real(source, destination)
    monkeypatch.setattr(module.os, 'replace', replace)
    result = restore(backup, target)
    assert (result['restored'], result['kept'], result['in_use']) == (3, 0, 1)
    assert (data / 'people/thumbnails/12.jpg').read_bytes() == b'fa'
    assert not list(data.rglob('*.restoring'))
    monkeypatch.setattr(module.os, 'replace', real)
    again = restore(backup, target)
    assert (again['restored'], again['kept'], again['in_use']) == (1, 3, 0)


@pytest.mark.parametrize('state', ['validation-only', 'activation-prepared'])
def test_a_library_that_is_not_activated_is_left_alone(sealed, monkeypatch, state):
    backup, manifest, tmp_path = sealed
    target, data = recovery(tmp_path, monkeypatch, backup, manifest, state=state)
    with pytest.raises(ExplainedFailure, match='Activate'):
        restore(backup, target)
    assert not list(data.iterdir())


def test_a_backup_made_before_thumbnails_were_kept_says_so(sealed, monkeypatch):
    backup, manifest, tmp_path = sealed
    target, data = recovery(tmp_path, monkeypatch, backup, manifest, listed=False)
    with pytest.raises(ExplainedFailure, match='repair-thumbnails'):
        restore(backup, target)
    assert not list(data.iterdir())


def test_a_copy_from_another_backup_or_a_damaged_one_restores_nothing(sealed, monkeypatch, tmp_path):
    backup, manifest, base = sealed
    target, data = recovery(base, monkeypatch, backup, manifest)
    monkeypatch.setattr('app.backup.library.verify_library_metadata', lambda directory, key, **kwargs: {'thumbnails': 'f' * 64})
    with pytest.raises(InvalidArtifact, match='another recovery point'):
        restore(backup, target)
    monkeypatch.setattr('app.backup.library.verify_library_metadata',
                        lambda directory, key, **kwargs: {'thumbnails': reference(manifest)})
    chunk = backup / module.FOLDER / '00000000.fernet'
    raw = bytearray(chunk.read_bytes())
    raw[len(raw) // 2] ^= 1
    chunk.write_bytes(raw)
    with pytest.raises(InvalidArtifact):
        restore(backup, target)
    assert not list(data.iterdir())


@pytest.mark.parametrize('name', ['../escape.jpg', 'thumbnails/../../escape.jpg', '/etc/thumbnails/a.jpg',
                                  'config/local.json', 'thumbnails/run.sh', 'thumbnails/a.jpg ', 'logs/a.jpg',
                                  'thumbnails\\a.jpg', 'C:/thumbnails/a.jpg', 'people/thumbnails/deep/a.jpg'])
def test_names_outside_the_cache_folders_are_refused(name):
    with pytest.raises(InvalidArtifact):
        module.cached_name(name)


def test_a_forged_stream_cannot_write_outside_the_cache(tmp_path, monkeypatch):
    import io
    from app.backup.encrypted import seal_stream
    name = b'../../config/local.json'
    body = module.HEADER.pack(len(name), 2) + name + b'{}'
    backup = tmp_path / 'backup'
    backup.mkdir()
    manifest = seal_stream(io.BytesIO(body), backup / module.FOLDER, KEY, estimated_bytes=len(body), guard_factory=Guard,
                           metadata={'kind': module.KIND, 'file_count': 1, 'file_bytes': 2})
    target, data = recovery(tmp_path, monkeypatch, backup, manifest)
    before = (target / 'catalog/files/config/local.json').read_bytes()
    with pytest.raises(InvalidArtifact):
        restore(backup, target)
    assert (target / 'catalog/files/config/local.json').read_bytes() == before and not list(data.iterdir())
