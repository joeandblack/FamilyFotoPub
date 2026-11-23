"""Destination path preflight: refuse before copying, and never rename anything."""
from contextlib import closing, contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from app.backup import encrypted_library, library, library_restore, media_paths, media_tree
from app.backup.media_inventory import capture_media_inventory
from app.backup.media_paths import check_paths, destination_limits, DestinationPathTooLong, PathLimits
from app.backup.media_tree import check_copy_paths, copy_media_tree
from app.backup.space import SpaceGuard
from tests.backup_keys import backup_key


def ample(requirements):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      disk_usage=lambda path: SimpleNamespace(free=10 ** 9))


def inventory_of(tmp_path, *names):
    media = tmp_path / 'media'
    media.mkdir()
    for name in names:
        path = media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
    directory = tmp_path / 'inventory'
    capture_media_inventory(media, directory, offline_confirmed=True, guard_factory=ample)
    return media, directory / 'inventory.sqlite3'


def length(path):
    return len(str(path))


def test_exact_file_limit_passes_and_one_more_is_refused(tmp_path):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    tree = tmp_path / 'tree'
    needed = length(tree) + 1 + len('photo.jpg')
    assert check_paths(inventory, tree, limits=PathLimits(file_units=needed)) == 1
    with pytest.raises(DestinationPathTooLong, match='shorter destination'):
        check_paths(inventory, tree, limits=PathLimits(file_units=needed - 1))


def test_directory_limit_is_stricter_than_the_file_limit(tmp_path):
    _, inventory = inventory_of(tmp_path, 'album/photo.jpg')
    tree = tmp_path / 'tree'
    directory = length(tree) + 1 + len('album')
    roomy = length(tree) + 200
    assert check_paths(inventory, tree, limits=PathLimits(file_units=roomy, directory_units=directory)) == 2
    with pytest.raises(DestinationPathTooLong):
        check_paths(inventory, tree, limits=PathLimits(file_units=roomy, directory_units=directory - 1))


def test_job_paths_count_even_when_the_media_path_fits(tmp_path):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    target = tmp_path / 'copy'
    # A hashed job path is longer than a short library file name.
    enough_for_media = length(target / 'media' / 'photo.jpg')
    assert check_paths(inventory, target / 'media', limits=PathLimits(file_units=enough_for_media)) == 1
    with pytest.raises(DestinationPathTooLong):
        check_copy_paths_with(inventory, target, PathLimits(file_units=enough_for_media))


def check_copy_paths_with(inventory, target, limits):
    return check_paths(inventory, target / 'media', jobs=target / 'file-jobs', limits=limits)


def test_job_requirement_is_exact(tmp_path):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    target = tmp_path / 'copy'
    needed = length(target / 'file-jobs') + 1 + 64 + 1 + len(media_paths.LONGEST_JOB_FILE)
    assert check_copy_paths_with(inventory, target, PathLimits(file_units=needed)) == 1
    with pytest.raises(DestinationPathTooLong):
        check_copy_paths_with(inventory, target, PathLimits(file_units=needed - 1))


def test_real_copy_check_includes_job_paths(tmp_path, monkeypatch):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    target = tmp_path / 'copy'
    fits_media_only = PathLimits(file_units=length(target / 'media' / 'photo.jpg'))
    monkeypatch.setattr(media_paths, 'destination_limits', lambda path: fits_media_only)
    with pytest.raises(DestinationPathTooLong):
        check_copy_paths(inventory, target)


def test_longest_job_file_really_is_the_longest_written(tmp_path):
    media, inventory = inventory_of(tmp_path, 'photo.jpg')
    copy_media_tree(media, inventory.parent, tmp_path / 'copy', offline_confirmed=True, guard_factory=ample)
    source = Path(media_tree.__file__.replace('media_tree.py', 'media_copy.py')).read_text(encoding='utf-8')
    for name in ('copy.lock', 'copy.json', 'copy.json.tmp', 'payload.part', 'payload'):
        assert name in source and len(name) <= len(media_paths.LONGEST_JOB_FILE)


def test_name_limit_counts_utf16_units_not_characters(tmp_path):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    roomy = PathLimits(file_units=10000)
    def entries(names):
        import sqlite3
        database = tmp_path / ('entries-' + str(len(list(tmp_path.iterdir()))) + '.sqlite3')
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE entries(path TEXT, kind TEXT)')
            db.executemany("INSERT INTO entries VALUES (?, 'file')", [(name,) for name in names])
        db.close()
        return database
    fits = 'a' * 251 + '.jpg'
    assert check_paths(entries([fits]), tmp_path / 'tree', limits=roomy) == 1
    with pytest.raises(DestinationPathTooLong, match='never renamed'):
        check_paths(entries(['a' * 252 + '.jpg']), tmp_path / 'tree', limits=roomy)
    # An emoji is one character but two UTF-16 units.
    with pytest.raises(DestinationPathTooLong, match='never renamed'):
        check_paths(entries(['\U0001F600' * 128]), tmp_path / 'tree', limits=roomy)
    assert check_paths(entries(['\U0001F600' * 127]), tmp_path / 'tree', limits=roomy) == 1


def test_byte_limits_apply_where_the_kernel_counts_bytes(tmp_path):
    import sqlite3
    database = tmp_path / 'chinese.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE entries(path TEXT, kind TEXT)')
        db.execute("INSERT INTO entries VALUES (?, 'file')", ('照' * 86,))
    db.close()
    # 86 units fit a UTF-16 filesystem limit, but encode to 258 UTF-8 bytes.
    assert check_paths(database, tmp_path / 'tree', limits=PathLimits(file_units=10000)) == 1
    if os.fsencode('照') == '照'.encode('utf-8'):
        with pytest.raises(DestinationPathTooLong, match='never renamed'):
            check_paths(database, tmp_path / 'tree', limits=PathLimits(file_units=10000, name_bytes=255))
        with pytest.raises(DestinationPathTooLong):
            check_paths(database, tmp_path / 'tree', limits=PathLimits(path_bytes=length(tmp_path)))


def test_refusal_reports_no_private_file_names(tmp_path):
    _, inventory = inventory_of(tmp_path, 'album/private-family-name.jpg')
    with pytest.raises(DestinationPathTooLong) as refused:
        check_paths(inventory, tmp_path / 'tree', limits=PathLimits(file_units=5, directory_units=5))
    assert 'private-family-name' not in str(refused.value) and 'album' not in str(refused.value)
    assert '2 paths' in str(refused.value)


@pytest.mark.parametrize('enabled,expected', [(False, True), (True, False)])
def test_windows_advice_matches_actual_capability(tmp_path, enabled, expected):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    with pytest.raises(DestinationPathTooLong) as refused:
        check_paths(inventory, tmp_path / 'tree', limits=PathLimits(file_units=5, long_paths=enabled))
    assert ('enable Windows long paths' in str(refused.value)) is expected


def test_copy_refuses_before_creating_anything_and_leaves_source_alone(tmp_path, monkeypatch):
    media, inventory = inventory_of(tmp_path, 'album/photo.jpg')
    before = {path: path.read_bytes() for path in media.rglob('*') if path.is_file()}
    monkeypatch.setattr(media_paths, 'destination_limits', lambda path: PathLimits(file_units=5, directory_units=5))
    target = tmp_path / 'copy'
    with pytest.raises(DestinationPathTooLong):
        copy_media_tree(media, inventory.parent, target, offline_confirmed=True, guard_factory=ample)
    assert not target.exists()
    assert {path: path.read_bytes() for path in media.rglob('*') if path.is_file()} == before


def test_resume_with_a_now_too_long_destination_keeps_earlier_work(tmp_path, monkeypatch):
    media, inventory = inventory_of(tmp_path, 'photo.jpg')
    target = tmp_path / 'copy'
    copy_media_tree(media, inventory.parent, target, offline_confirmed=True, guard_factory=ample)
    existing = sorted(path for path in target.rglob('*'))
    monkeypatch.setattr(media_paths, 'destination_limits', lambda path: PathLimits(file_units=5, directory_units=5))
    with pytest.raises(DestinationPathTooLong):
        copy_media_tree(media, inventory.parent, target, offline_confirmed=True, guard_factory=ample)
    assert sorted(path for path in target.rglob('*')) == existing


def test_copy_within_limits_succeeds_with_real_limits(tmp_path):
    media, inventory = inventory_of(tmp_path, 'album/photo.jpg')
    result = copy_media_tree(media, inventory.parent, tmp_path / 'copy', offline_confirmed=True, guard_factory=ample)
    assert result['files'] == 1
    assert (tmp_path / 'copy/media/album/photo.jpg').read_bytes() == b'fixture'


def test_capture_refuses_before_the_database_dump(tmp_path, monkeypatch):
    @contextmanager
    def frozen(endpoint):
        yield object()

    def forbidden(*args, **kwargs):
        pytest.fail('Too-long destination must be refused before the dump and copy')

    config, data, media = (tmp_path / name for name in ('config', 'data', 'media'))
    for path in (config, data, media):
        path.mkdir()
    (media / 'photo.jpg').write_bytes(b'fixture')
    monkeypatch.setattr(library, 'freeze_database', frozen)
    monkeypatch.setattr(library, 'capture_components', forbidden)
    monkeypatch.setattr(library, 'copy_media_tree', forbidden)
    monkeypatch.setattr(library, 'reconcile_uploads', lambda db, root: None)
    monkeypatch.setattr(library, 'reconcile_media', lambda db, root: {})
    monkeypatch.setattr(library, 'state_inventory', lambda roots: [])
    monkeypatch.setattr(media_paths, 'destination_limits', lambda path: PathLimits(file_units=5, directory_units=5))
    with pytest.raises(DestinationPathTooLong):
        library.capture_library(object(), None, config, data, media, tmp_path / 'backup', backup_key(),
                                database_budget=1024, offline_confirmed=True, guard_factory=ample)


def test_plain_restore_refuses_before_the_catalog(tmp_path, monkeypatch):
    media, inventory = inventory_of(tmp_path, 'photo.jpg')
    backup = tmp_path / 'backup/media-copy/inventory'
    backup.mkdir(parents=True)
    (backup / 'inventory.sqlite3').write_bytes(inventory.read_bytes())
    def forbidden(*args, **kwargs):
        pytest.fail('Too-long destination must be refused before the catalog restore')
    monkeypatch.setattr(library_restore, 'restore_catalog', forbidden)
    monkeypatch.setattr(media_paths, 'destination_limits', lambda path: PathLimits(file_units=5, directory_units=5))
    with pytest.raises(DestinationPathTooLong):
        library_restore._restore_contents(tmp_path / 'backup', b'', tmp_path / 'recovered', None, None, {}, None, 'a' * 32,
                                          expected_cluster_directory=tmp_path, database_budget=1,
                                          guard_factory=ample, progress=None)


def test_encrypted_restore_refuses_before_restoring_media(tmp_path, monkeypatch):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    metadata = tmp_path / 'metadata-payload'
    (metadata / 'inventory').mkdir(parents=True)
    (metadata / 'inventory/inventory.sqlite3').write_bytes(inventory.read_bytes())
    payload = {'id': 'a' * 32, 'media_snapshot': 'b' * 64, 'media_root': '/media', 'media_bytes': 7, 'media_files': 1}
    jobs = []
    def job(engine, snapshot, repository, directory, **kwargs):
        jobs.append(directory.name)
        return metadata
    monkeypatch.setattr(encrypted_library, 'restore_snapshot_job', job)
    monkeypatch.setattr(encrypted_library, 'verify_metadata', lambda *args: payload)
    monkeypatch.setattr(encrypted_library, 'unlock_context', lambda folder, password: b'key')
    monkeypatch.setattr(encrypted_library, 'verify_artifact', lambda *args: {'metadata': {'file_bytes': 1}})
    monkeypatch.setattr(media_paths, 'destination_limits', lambda path: PathLimits(file_units=5, directory_units=5))
    engine = SimpleNamespace(snapshot=lambda snapshot: {'tags': ['familyfoto:' + 'a' * 32], 'restore_root': '/media'})
    tmp_target = tmp_path / 'recovered'
    tmp_target.mkdir()
    with pytest.raises(DestinationPathTooLong):
        encrypted_library._restore_encrypted_contents(
            engine, 'c' * 64, 'd' * 64, b'', tmp_target, None, None, expected_cluster_directory=tmp_path,
            database_budget=1, metadata_budget=1, guard_factory=ample, progress=None)
    assert jobs == ['metadata-job']


@pytest.mark.skipif(os.name != 'nt', reason='Measures this Windows filesystem')
def test_windows_limits_match_the_actual_filesystem(tmp_path):
    """The model must agree with what this machine's filesystem really accepts."""
    limits = destination_limits(tmp_path)
    if limits.long_paths:
        deep = tmp_path / ('a' * 200) / ('b' * 200)
        deep.mkdir(parents=True)
        (deep / 'file').write_bytes(b'x')
        assert length(deep / 'file') > media_paths.WINDOWS_FILE_UNITS
        return
    assert limits.file_units == media_paths.WINDOWS_FILE_UNITS
    assert limits.directory_units == media_paths.WINDOWS_DIRECTORY_UNITS
    base = tmp_path / ('d' * 100)
    base.mkdir()
    parent = base / ('e' * (limits.directory_units - length(base) - 1))
    assert length(parent) == limits.directory_units
    parent.mkdir()
    with pytest.raises(OSError):
        (base / ('f' * (limits.directory_units - length(base)))).mkdir()
    fits = parent / ('g' * (limits.file_units - length(parent) - 1))
    assert length(fits) == limits.file_units
    fits.write_bytes(b'x')
    with pytest.raises(OSError):
        (parent / ('h' * (limits.file_units - length(parent)))).write_bytes(b'x')


@pytest.mark.skipif(os.name == 'nt', reason='Measures POSIX pathconf')
def test_posix_limits_come_from_the_destination(tmp_path):
    limits = destination_limits(tmp_path / 'not-created-yet/deeper')
    assert limits.name_bytes == (None if sys.platform == 'darwin' else os.pathconf(tmp_path, 'PC_NAME_MAX'))
    assert limits.path_bytes == os.pathconf(tmp_path, 'PC_PATH_MAX') - 1
    assert limits.file_units is None and limits.long_paths is None


@pytest.mark.parametrize('character', ['a', '照', '\U0001F600'], ids=['ascii', 'cjk', 'emoji'])
def test_name_limit_matches_the_actual_filesystem(tmp_path, character):
    """Accept the longest name this filesystem really creates, and refuse one more character."""
    probe = tmp_path / 'probe'
    probe.mkdir()
    # The prefix keeps Windows MAX_PATH out of a measurement of the name limit alone.
    probing = '\\\\?\\' + str(probe) if os.name == 'nt' else str(probe)
    longest = 0
    for count in range(1, media_paths.NAME_UNITS + 2):
        name = character * count
        path = os.path.join(probing, name)
        try:
            with open(path, 'wb'):
                pass
        except OSError:
            break
        if os.listdir(probing) != [name]:
            pytest.skip('This filesystem normalizes Unicode names')
        os.remove(path)
        longest = count
    assert longest

    tree = tmp_path / 'tree'
    limits = destination_limits(tree)
    if os.name == 'nt':
        limits = replace(limits, file_units=None, directory_units=None)

    def inventory(count):
        directory = tmp_path / f'inventory-{count}'
        directory.mkdir()
        database = directory / 'inventory.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE entries(path TEXT, kind TEXT)')
            db.execute('INSERT INTO entries VALUES (?,?)', (character * count, 'file'))
            db.commit()
        return database

    assert check_paths(inventory(longest), tree, limits=limits) == 1
    with pytest.raises(DestinationPathTooLong, match='name too long'):
        check_paths(inventory(longest + 1), tree, limits=limits)


def refusing_pathconf(path, name):
    raise OSError(22, 'Invalid argument')


def test_macos_exfat_falls_back_to_the_kernel_path_limit(tmp_path, monkeypatch):
    """macOS exFAT raises for PC_PATH_MAX, which must not end the backup with a bare error."""
    monkeypatch.setattr(media_paths.os, 'pathconf', refusing_pathconf, raising=False)
    monkeypatch.setattr(media_paths.sys, 'platform', 'darwin')
    assert media_paths.posix_path_bytes(tmp_path) == 1023


def test_other_systems_do_not_guess_a_path_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(media_paths.os, 'pathconf', refusing_pathconf, raising=False)
    monkeypatch.setattr(media_paths.sys, 'platform', 'linux')
    with pytest.raises(OSError):
        media_paths.posix_path_bytes(tmp_path)


def test_a_measured_path_limit_wins_over_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(media_paths.os, 'pathconf', lambda path, name: 4096, raising=False)
    monkeypatch.setattr(media_paths.sys, 'platform', 'darwin')
    assert media_paths.posix_path_bytes(tmp_path) == 4095


def test_check_copy_paths_uses_measured_limits(tmp_path, monkeypatch):
    _, inventory = inventory_of(tmp_path, 'photo.jpg')
    seen = []
    def measured(path):
        seen.append(Path(path))
        return PathLimits(file_units=10000, directory_units=10000)
    monkeypatch.setattr(media_paths, 'destination_limits', measured)
    assert check_copy_paths(inventory, tmp_path / 'copy') == 1
    assert seen == [tmp_path / 'copy' / 'media']
