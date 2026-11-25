"""Restore a trusted whole-library backup into new, validation-only storage."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import uuid
from itertools import zip_longest

from .diagnostics import ExplainedFailure
from .encrypted import verify_artifact, InvalidArtifact, read_bounded
from .library import (MOVED_FORMAT, MOVED_RECORD, media_moved, moved_record, thumbnails_state, verify_library,
                      verify_library_metadata)
from .media_copy import sync_directory, copy_lock
from .media_inventory import capture_media_inventory, verify_media_inventory
from .media_receipt import write_receipt
from .media_tree import check_copy_paths, copy_media_tree, verify_media_tree, verify_tree, file_digest
from .passwords import publish as publish_password, require_context
from .private_storage import check_destination_privacy, create_private_directory
from .recovery import restore_catalog, save_private
from .space import Requirement, SpaceGuard, directory_identity
from .volumes import same_except_device, same_job, volume_identity
from .identity import read_library_identity
from .retirement import verify_retirement


def equivalent_inventories(expected, observed):
    """Compare content/layout, not source-machine inode or directory sizes."""
    for path in (expected, observed):
        file_digest(path)  # Reject linked/missing metadata and linked parents.
    with closing(sqlite3.connect(expected.as_uri() + '?mode=ro', uri=True)) as left:
        with closing(sqlite3.connect(observed.as_uri() + '?mode=ro', uri=True)) as right:
            query = ("SELECT path,kind,CASE WHEN kind='file' THEN size ELSE 0 END,sha256 "
                     'FROM entries ORDER BY path')
            for db in (left, right):
                db.execute('PRAGMA trusted_schema=OFF')
                db.execute('BEGIN')
            for old, new in zip_longest(left.execute(query), right.execute(query)):
                if old != new:
                    raise InvalidArtifact('Restored media inventory differs from the authenticated backup')


@contextmanager
def authenticated_digests(inventory):
    """Digests from the backup's sealed inventory, for files the same restore has just hashed."""
    with closing(sqlite3.connect(inventory.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')

        def known(name, info):
            row = db.execute("SELECT size,sha256 FROM entries WHERE path=? AND kind='file'", (name,)).fetchone()
            if row is None or row[0] != info.st_size:
                raise InvalidArtifact('Backup media differs from its authenticated inventory')
            return row[1]
        yield known


def restore_library(directory, key, target_directory, admin, pg_restore, *, expected_cluster_directory,
                    database_budget, resume=False, guard_factory=SpaceGuard, progress=None,
                    validation_follows=False, in_place=False, media_directory=None, allow_unprotected_media=False):
    """No service starts, disk cutover, unrelated target reuse or source deletion.

    `validation_follows` is for a caller that runs validate_plain_candidate with
    `fresh_restore` straight after. It still hashes the backup first, and every file as it
    is copied and read back, and leaves the full check of the restored copy to validation.

    `in_place` moves the backup's media into the recovery folder instead of copying it, so
    a library nearly as large as its disk can be restored there. The backup is used up: it
    keeps its catalog and a record of where its media went, and can't be restored again.
    The media is fully verified before it is moved, and the move is one rename on one disk.

    `media_directory` splits a restore: the media goes into that new folder, and the recovery
    folder, with the catalog, settings and credentials, can be on another, protected disk.
    In place, the media folder must be on the backup's disk, because the media is moved by one
    rename. A copy puts the media there on any disk. Only the media folder may be on a disk
    that keeps no permissions, and only with `allow_unprotected_media`.
    """
    directory, target_directory = (Path(os.path.abspath(p)) for p in (directory, target_directory))
    if directory.is_relative_to(target_directory) or target_directory.is_relative_to(directory):
        raise ValueError('Recovery destination must be separate from backup')
    if allow_unprotected_media and media_directory is None:
        raise ValueError('Only a separate media folder may be on an unprotected disk')
    split = media_directory is not None
    home = Path(os.path.abspath(media_directory)) if split else target_directory
    if split:
        for other in (directory, target_directory):
            if home.is_relative_to(other) or other.is_relative_to(home):
                raise ValueError('The media folder must be separate from the backup and the recovery folder')
        if home.exists() and not resume:
            raise FileExistsError('A split restore needs a new media folder, or an explicit resume')
    if target_directory.exists() and not resume:
        raise FileExistsError('Whole-library restore requires a new target or explicit resume')
    moved = media_moved(directory)
    if moved is not None and not (in_place and resume and moved['target'] == str(target_directory)
                                  and moved.get('media_directory', moved['target']) == str(home)):
        place = moved.get('media_directory', moved['target'])
        raise ExplainedFailure(f"This backup's photos were moved into {place} by an in-place restore, "
                               'so it is no longer a backup and cannot be restored again. To continue that restore, '
                               'repeat it with --in-place --resume and the same target folder'
                               + (f' and --media-directory {place}.' if place != moved['target'] else '.'))
    if in_place:
        # One rename on one volume, never a copy: the rename itself refuses another disk too.
        if volume_identity(home.parent) != volume_identity(directory):
            what = 'media folder' if split else 'new folder'
            raise ExplainedFailure(f'An in-place restore needs the {what} on the same disk as the backup. '
                                   'Choose a folder on that disk, or restore normally to another disk.')
    if not resume:
        # Refuse an unprotected disk before any long check, and before either folder exists.
        check_destination_privacy(target_directory.parent)
        if split:
            check_destination_privacy(home.parent, allow_unprotected=allow_unprotected_media)
    read = {}
    payload = verify_library(directory, key, progress, media_result=read)
    retirement = verify_retirement(directory, key, payload) if payload.get('migration_target') else None
    state = verify_artifact(directory / 'catalog/state', key)
    media_budget = 0 if in_place else payload['media_bytes'] + payload['media_files'] * 32768
    files_budget = state['metadata']['file_bytes'] + 1024 ** 2
    required = [Requirement('recovered-library', target_directory.parent, files_budget + (0 if split else media_budget)),
                Requirement('database', Path(expected_cluster_directory), database_budget)]
    if split and media_budget:
        required.append(Requirement('recovered-media', home.parent, media_budget))
    guard_factory(required).check()
    if resume:
        directory_identity(target_directory)
    else:
        # Always protected: it holds the catalog, settings and credentials.
        create_private_directory(target_directory)
        # The backup's public password context, so this work can be unlocked again on its own.
        publish_password(target_directory, key)
    if split and not resume:
        create_private_directory(home, allow_unprotected=allow_unprotected_media)
    with copy_lock(target_directory):
        expected = {'format': 'familyfoto-plain-library-restore-job-v1', 'capture_id': payload['id'],
                    'password': key.binding(),
                    'media_inventory_sha256': payload['media_inventory_sha256'], 'backup': str(directory),
                    'identity': list(directory_identity(target_directory)), 'volume': volume_identity(target_directory),
                    'cluster': str(Path(expected_cluster_directory).resolve()),
                    'endpoint': [admin.host, admin.port, admin.user], 'activation_allowed': False}
        if in_place:
            # Only an in-place job carries this, so a resume can't switch modes.
            expected['media_in_place'] = True
        job = target_directory / 'restore-job.json'
        if resume and not job.exists():
            raise ExplainedFailure('This restore stopped before it recorded any work, so it cannot continue. '
                                   'Nothing was moved or lost: start again in a new folder'
                                   + (', with a new media folder.' if split else '.'))
        if split and not (home.is_dir() and not home.is_symlink()):
            # It was made before the job was recorded, and the media may have moved into it.
            raise InvalidArtifact('The media folder of this restore is missing; review required')
        if resume:
            require_context(target_directory, key)
        media_identity = list(directory_identity(home)) if split else None
        if split:
            expected.update(media_directory=str(home), media_volume=volume_identity(home))
        if resume:
            record = json.loads(read_bounded(job, 65536))
            if isinstance(record, dict) and (record.get('media_in_place') != expected.get('media_in_place')
                                             or record.get('media_directory') != expected.get('media_directory')):
                started = '--in-place' if record.get('media_in_place') else 'a normal restore'
                if isinstance(record.get('media_directory'), str):
                    started += f" and --media-directory {record['media_directory']}"
                raise ExplainedFailure(f'This restore was started with {started}. Resume it the same way, with --resume.')
            if (not isinstance(record, dict) or record.get('media_in_place') != expected.get('media_in_place')
                    or record.get('media_directory') != expected.get('media_directory')
                    or (split and not same_except_device(record.get('media_identity'), media_identity))
                    or not same_job({k: record.get(k) for k in expected}, expected, 'identity')):
                raise InvalidArtifact('Whole-library recovery job identity differs')
            restoration_id = record.get('restoration_id')
            try:
                valid_id = uuid.UUID(hex=restoration_id).hex == restoration_id
            except (ValueError, AttributeError, TypeError):
                valid_id = False
            if not valid_id or (payload.get('migration_target') and payload['migration_target'] != restoration_id):
                raise InvalidArtifact('Whole-library restoration identity differs')
            if (not (target_directory / 'LIBRARY-RESTORE-INCOMPLETE').is_file()
                    or (target_directory / 'LIBRARY-RECOVERY-REPORT.json').exists()):
                raise InvalidArtifact('Only an incomplete whole-library restore can be resumed')
        else:
            restoration_id = payload.get('migration_target') or uuid.uuid4().hex
            extra = {'media_identity': media_identity} if split else {}
            save_private(job, {**expected, **extra, 'restoration_id': restoration_id})
            with (target_directory / 'LIBRARY-RESTORE-INCOMPLETE').open('xb') as stream:
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(target_directory)
        return _restore_contents(directory, key, target_directory, admin, pg_restore, payload, retirement, restoration_id,
            expected_cluster_directory=expected_cluster_directory, database_budget=database_budget,
            guard_factory=guard_factory, progress=progress, validation_follows=validation_follows, in_place=in_place,
            home=home, read=read, allow_unprotected_media=allow_unprotected_media)


def recovered_media_copy(target_directory):
    """Where a plain recovery keeps its media tree: in the recovery folder, or in the separate
    media folder its job names. Readers still check the tree against the sealed inventory."""
    target_directory = Path(target_directory)
    job = target_directory / 'restore-job.json'
    if job.is_file() and not job.is_symlink():
        value = json.loads(read_bounded(job, 65536))
        if isinstance(value, dict) and 'media_directory' in value:
            media = value['media_directory']
            if not isinstance(media, str) or not Path(media).is_absolute():
                raise InvalidArtifact('Invalid media folder in recovery job')
            if (not same_except_device(value.get('media_identity'), directory_identity(Path(media)))
                    or value.get('media_volume') != volume_identity(Path(media))):
                raise InvalidArtifact('The media folder is not the one this restore made')
            return Path(media) / 'media-copy'
    return target_directory / 'media-copy'


def move_media_in_place(directory, target_directory, payload, restoration_id, progress=None, home=None):
    """Move the verified backup media into the recovery folder, or a split restore's media
    folder (`home`), with one rename.

    The record is written first, so a stop before or after the rename resumes. A record
    whose media is still in the backup is from a rename that failed, and is replaced. After
    the rename only names and sizes are rechecked: renaming a folder doesn't touch its files,
    and every reader rereads them against the sealed inventory before trusting them.
    """
    home = target_directory if home is None else home
    source, target = directory / 'media-copy', home / 'media-copy'
    record = {'format': MOVED_FORMAT, 'target': str(target_directory), 'restoration_id': restoration_id,
              'capture_id': payload['id'], 'media_inventory_sha256': payload['media_inventory_sha256']}
    if home != target_directory:
        record['media_directory'] = str(home)
    existing = moved_record(directory)
    if existing != record:
        if media_moved(directory) is not None:
            raise InvalidArtifact('Moved-media record belongs to another restore')
        save_private(directory / MOVED_RECORD, record)
        sync_directory(directory)
    if source.exists() or source.is_symlink():
        if target.exists() or target.is_symlink():
            raise InvalidArtifact('Both the backup and the recovery folder hold media; review required')
        if progress:
            progress({'phase': 'media-move'})
        # os.rename never copies: across volumes it fails instead.
        os.rename(source, target)
        sync_directory(directory)
        sync_directory(home)
    elif not target.is_dir() or target.is_symlink():
        raise InvalidArtifact("The backup's media is in neither the backup nor the recovery folder")
    snapshot = target / 'inventory/inventory.sqlite3'
    if file_digest(snapshot) != payload['media_inventory_sha256']:
        raise InvalidArtifact('Moved media inventory differs from the authenticated backup')
    with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        verify_tree(db, target / 'media', content=False)


def _restore_contents(directory, key, target_directory, admin, pg_restore, payload, retirement, restoration_id, *,
                       expected_cluster_directory, database_budget, guard_factory, progress, validation_follows=False,
                       in_place=False, home=None, read=None, allow_unprotected_media=False):
    home = target_directory if home is None else home
    marker = target_directory / 'LIBRARY-RESTORE-INCOMPLETE'
    if not in_place:
        # The authenticated inventory already names every path this restore will write.
        check_copy_paths(directory / 'media-copy/inventory/inventory.sqlite3', home / 'media-copy')
    catalog = restore_catalog(directory / 'catalog', key, target_directory / 'catalog', admin, pg_restore,
                              expected_cluster_directory=expected_cluster_directory, database_budget=database_budget,
                              resume=(target_directory / 'catalog').exists(), guard_factory=guard_factory, progress=progress)
    if payload.get('library_id') is not None:
        if read_library_identity(target_directory / 'catalog/files/data') != payload['library_id']:
            raise InvalidArtifact('Restored library identity does not match backup')
    if in_place:
        move_media_in_place(directory, target_directory, payload, restoration_id, progress, home)
        # Every file was read against the inventory just before the move, and the move is one
        # rename. So a walk that finds the same names, sizes and times stands for that read,
        # and earns the receipt that spares every later step another one. Anything else
        # is read in full again.
        media = verify_media_tree(home / 'media-copy', content=False)
        if read and all(media[field] == read.get(field)
                        for field in ('files', 'bytes', 'tree_signature', 'inventory_sha256')):
            media = {**media, 'content_verified': True}
        else:
            media = verify_media_tree(home / 'media-copy', progress)
        if (media['files'], media['bytes']) != (payload['media_files'], payload['media_bytes']):
            raise InvalidArtifact('Moved media totals differ from the authenticated backup')
        write_receipt(target_directory, key, home / 'media-copy', media)
        if verify_library_metadata(directory, key) != payload:
            raise InvalidArtifact('Backup changed during recovery')
        return _finish(target_directory, payload, catalog, retirement, restoration_id, marker, in_place=True, home=home,
                       thumbnails=thumbnails_state(directory, key, payload))
    if progress:
        progress({'phase': 'media-inventory'})
    source = directory / 'media-copy/media'
    expected = directory / 'media-copy/inventory/inventory.sqlite3'
    inventory = target_directory / 'media-inventory'
    if inventory.exists() and (inventory / 'INVENTORY-INCOMPLETE').exists():
        if (target_directory / 'media-copy').exists():
            raise InvalidArtifact('Cannot replace an inventory already bound to media copying')
        directory_identity(inventory)
        inventory.rename(target_directory / ('previous-inventory-' + uuid.uuid4().hex))
    if inventory.exists():
        verify_media_inventory(inventory, source, rehash=not validation_follows)
    elif validation_follows:
        # The copy below still hashes each file against these digests as it reads it.
        with authenticated_digests(expected) as known:
            capture_media_inventory(source, inventory, offline_confirmed=True, guard_factory=guard_factory,
                                    progress=progress, known_digest=known, authenticated=expected)
    else:
        capture_media_inventory(source, inventory, offline_confirmed=True, guard_factory=guard_factory,
                                progress=progress, authenticated=expected)
    equivalent_inventories(expected, inventory / 'inventory.sqlite3')
    copy_media_tree(source, inventory, home / 'media-copy', offline_confirmed=True,
                    guard_factory=guard_factory, progress=progress, allow_unprotected=allow_unprotected_media)
    if validation_follows:
        if verify_library_metadata(directory, key) != payload:
            raise InvalidArtifact('Backup changed during recovery')
    else:
        write_receipt(target_directory, key, home / 'media-copy', verify_media_tree(home / 'media-copy', progress))
        # A changed backup cannot lend its earlier successful verification to this restore.
        checked = verify_library(directory, key, progress)
        if checked != payload:
            raise InvalidArtifact('Backup changed during recovery')
    return _finish(target_directory, payload, catalog, retirement, restoration_id, marker, home=home,
                   thumbnails=thumbnails_state(directory, key, payload))


def _finish(target_directory, payload, catalog, retirement, restoration_id, marker, *, in_place=False, home=None,
            thumbnails='none'):
    home = target_directory if home is None else home
    media_root = home / 'media-copy/media'
    config_path = target_directory / 'catalog/files/config/local.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['recovery'].update(media_verified=True, media_root=str(media_root),
                              media_inventory_sha256=payload['media_inventory_sha256'])
    save_private(config_path, config)
    report = {**catalog, 'media_verified': True, 'media_root': str(media_root),
              'library_id': payload.get('library_id'),
              'restoration_id': restoration_id,
              'media_files': payload['media_files'], 'media_bytes': payload['media_bytes'],
              'source_retired': retirement is not None, 'activation_allowed': False,
              'media_in_place': in_place, 'media_directory': str(home),
              'state': 'validation-only', 'media_inventory_sha256': payload['media_inventory_sha256'],
              # A cache, checked only to say so. 'damaged' never stops a restore: rebuild them instead.
              'thumbnails': thumbnails}
    save_private(target_directory / 'catalog/RECOVERY-REPORT.json', report)
    save_private(target_directory / 'LIBRARY-RECOVERY-REPORT.json', report)
    marker.unlink()
    sync_directory(target_directory)
    return report
