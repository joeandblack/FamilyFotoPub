"""One offline recovery point: encrypted catalog plus authenticated plain media."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid

from cryptography.fernet import Fernet, InvalidToken

from .admission import maintenance, MaintenanceBlocked
from .catalog import capture_components, publish_catalog, freeze_database, reference, verify_catalog, journal
from .encrypted import read_bounded, seal_stream, verify_artifact, read_chunks, storage_budget, InvalidArtifact
from .media_copy import sync_directory
from .passwords import (BackupKey, FILE as PASSWORD_FILE, PART as PASSWORD_PART, publish as publish_password,
                        require_context, validate_context_binding)
from .media_inventory import (capture_media_inventory, hash_file, portable_path, recorded_name, signature,
                              verify_media_inventory, InvalidMediaInventory)
from .media_tree import check_copy_paths, copy_media_tree, remaining_media_bytes, verify_media_tree, file_digest
from .private_storage import check_destination_privacy, create_private_directory
from .recovery import abandoned_publication, save_private
from .sidecars import is_sidecar
from .space import directory_identity, SpaceGuard, Requirement
from .state_files import state_inventory
from .handover import FOLDER as HANDOVER, write_handover
from .windows_handover import FOLDER as WINDOWS_HANDOVER
from .identity import ensure_library_identity, read_library_identity
from .retirement import successor_id, mark_source_retired, publish_retirement
from .uploads import reconcile_uploads
from .thumbnails import FOLDER as THUMBNAILS, capture_thumbnails, thumbnail_budget, verify_thumbnails
from .timing import RECORD as TIMING, Timing
from .volumes import same_except_device, volume_identity


FORMAT = 'familyfoto-library-plain-media-v2'
JOB_FORMAT = 'familyfoto-library-capture-job-v1'
JOB_RECORD = 'capture-job.json'
CAPTURE_ENTRIES = {PASSWORD_FILE, PASSWORD_PART, JOB_RECORD, 'journal.json', 'journal.part', 'inventory', 'catalog', 'media-copy', 'library',
                   THUMBNAILS, TIMING, HANDOVER, WINDOWS_HANDOVER}
# An in-place restore moves the backup's media into its recovery folder and leaves this behind.
MOVED_RECORD = 'MEDIA-MOVED.json'
MOVED_FORMAT = 'familyfoto-media-moved-v1'


def moved_record(directory):
    """Where an in-place restore moved this backup's media, or None if it was never moved."""
    path = Path(directory) / MOVED_RECORD
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise InvalidArtifact('Invalid moved-media record')
    try:
        value = json.loads(read_bounded(path, 16384))
    except ValueError:
        raise InvalidArtifact('Invalid moved-media record') from None
    if not isinstance(value, dict):
        raise InvalidArtifact('Invalid moved-media record')
    target, restoration = value.get('target'), value.get('restoration_id')
    media = value.get('media_directory', target)
    if (value.get('format') != MOVED_FORMAT or not isinstance(target, str) or not Path(target).is_absolute()
            or not isinstance(media, str) or not Path(media).is_absolute()
            or not isinstance(restoration, str) or not re.fullmatch('[0-9a-f]{32}', restoration)):
        raise InvalidArtifact('Invalid moved-media record')
    return value


def media_moved(directory):
    """The record of a move that happened. A record whose media is still here is from a
    rename that failed: the backup is still whole, and the record proves nothing."""
    record = moved_record(directory)
    here = Path(directory) / 'media-copy'
    return None if record is None or here.exists() or here.is_symlink() else record


def media_copy_of(directory):
    """The backup's media tree: in the backup, or where an in-place restore moved it.

    Its location proves nothing. Every reader still checks the tree against the inventory
    digest sealed in the backup's own library index.
    """
    directory = Path(directory)
    record = media_moved(directory)
    if record is None:
        return directory / 'media-copy'
    # The recovery folder, or the separate media folder of a split restore.
    return Path(record.get('media_directory', record['target'])) / 'media-copy'


def reconcile_media(db, inventory):
    """Compare frozen catalog references with hashed physical files, bounded-memory."""
    counts = {'catalog_media': 0, 'retained_originals': 0, 'removed_originals': 0}
    with closing(sqlite3.connect((Path(inventory) / 'inventory.sqlite3').as_uri() + '?mode=ro', uri=True)) as files:
        files.execute('PRAGMA trusted_schema=OFF')
        def check(name, size, digest, required=True):
            portable_path(name)
            stored = recorded_name(files, name)
            item = None if stored is None else files.execute(
                "SELECT size,sha256 FROM entries WHERE path=? AND kind='file'", (stored,)).fetchone()
            if item is None and not required:
                return False
            if item != (size, digest):
                raise InvalidMediaInventory('Catalog file is missing or does not match its recorded size/checksum')
            return True
        with db.cursor(name='backup_media_reconciliation') as rows:
            rows.itersize = 1000
            rows.execute("SELECT m.relative_path,m.size,coalesce(m.stored_sha256,m.sha256),"
                         "m.original_relative_path,m.original_size,m.sha256,m.conversion_state,"
                         "v.metadata,t.deleting_at FROM media m "
                         "LEFT JOIN media_trash t ON t.media_id=m.id "
                         "LEFT JOIN video_metadata v ON v.media_id=m.id WHERE t.deleted_at IS NULL")
            for path, size, digest, original, original_size, original_digest, conversion, metadata, deleting in rows:
                if deleting is not None:
                    raise InvalidMediaInventory('Finish or review interrupted media deletion before backup')
                check(path, size, digest)
                counts['catalog_media'] += 1
                if original and original != path:
                    validation = (metadata or {}).get('conversion_validation') or {}
                    if validation.get('original_delete_started_at') and not validation.get('original_deleted_at'):
                        raise InvalidMediaInventory('Finish or review interrupted original deletion before backup')
                    # Legacy AVI conversion could remove the managed original
                    # without a deletion receipt. FLV/MTS require the explicit receipt.
                    removed = bool(validation.get('original_deleted_at')) or (
                        original.lower().endswith('.avi') and conversion == 'done')
                    retained = check(original, original_size, original_digest, required=not removed)
                    counts['retained_originals' if retained else 'removed_originals'] += 1
    return counts


def capture_binding(config_root, data_root, media_root, directory, retire_to):
    """What a retry must find unchanged before it may reuse anything in this directory."""
    roots = {'config': config_root, 'data': data_root, 'media': media_root, 'backup': directory}
    return {'format': JOB_FORMAT, 'retire_to': retire_to,
            'roots': {name: {'path': str(path), 'identity': list(directory_identity(path)),
                             'volume': volume_identity(path)} for name, path in roots.items()}}


def same_binding(job, binding):
    """Same disks and folders. A remount may change a device number, but no volume or inode."""
    roots = job.get('roots')
    if (job.get('format') != binding['format'] or job.get('retire_to') != binding['retire_to']
            or not isinstance(roots, dict) or set(roots) != set(binding['roots'])):
        return False
    return all(isinstance(roots[name], dict) and roots[name].get('path') == root['path']
               and roots[name].get('volume') == root['volume']
               and same_except_device(roots[name].get('identity'), root['identity'])
               for name, root in binding['roots'].items())


def write_capture_job(directory, key, binding, identity, library_id, *, engine=None):
    """Seal the shared ownership record without changing engine-specific retry policy."""
    job = {**binding, 'id': identity, 'library_id': library_id}
    if engine is not None:
        job['engine'] = engine
    save_private(directory / JOB_RECORD,
                 {'sealed': Fernet(key).encrypt(json.dumps(job).encode()).decode()})


def publish_library_index(directory, key, identity, library_id, catalog, media, counts,
                          thumbnails, *, guard_factory=SpaceGuard, retire_to=None):
    """Publish the common recovery index; callers retain admission and completion policy."""
    payload = {'format': FORMAT, 'id': identity, 'library_id': library_id, 'catalog': reference(catalog),
               'media_inventory_sha256': media['inventory_sha256'], 'media_files': media['files'],
               'media_bytes': media['bytes'], 'reconciliation': counts, 'media_encrypted': False,
               'password': key.binding(), 'capture_mode': 'offline', 'activation_allowed': False}
    if retire_to is not None:
        payload['migration_target'] = retire_to
    if thumbnails is not None:
        payload['thumbnails'] = reference(thumbnails)
    if not media['content_verified']:
        # Standard single-pass copies still need destination read-back before restore.
        payload['media_read_back'] = False
    write_handover(directory)
    raw = json.dumps(payload, sort_keys=True).encode()
    seal_stream(io.BytesIO(raw), directory / 'library', key, estimated_bytes=len(raw),
                metadata={'kind': FORMAT}, guard_factory=guard_factory)
    return payload


def resumable_capture(config_root, data_root, media_root, directory, key, retire_to):
    """Refuse, before taking the gate, anything but this same interrupted capture."""
    retired = data_root / '.recovery-admission/retired.json'
    if retired.exists() or retired.is_symlink():
        raise MaintenanceBlocked('This source is retired; finish its receipt with recover-retirement instead')
    if not directory.is_dir():
        raise FileNotFoundError('No interrupted capture in this directory')
    for entry in directory.iterdir():
        if entry.name not in CAPTURE_ENTRIES and is_sidecar(entry, needs_sibling=False):
            continue
        if ((entry.name not in CAPTURE_ENTRIES
             and not re.fullmatch('previous-(catalog|library|inventory|media-copy|thumbnails)-[0-9a-f]{32}', entry.name)
             and not abandoned_publication(entry, (JOB_RECORD, TIMING)))
                or entry.is_symlink() or entry.is_junction()):
            raise InvalidMediaInventory('Unowned capture directory; review required')
    record = directory / JOB_RECORD
    if record.is_symlink() or not record.is_file():
        raise InvalidArtifact('This directory has no retry record; capture into a new directory')
    # The same backup password context, before its sealed retry record is opened with this key.
    require_context(directory, key)
    try:
        sealed = json.loads(read_bounded(record, 65536))['sealed'].encode()
        job = json.loads(Fernet(key).decrypt(sealed))
        identity = job['id']
    except (InvalidToken, KeyError, TypeError, AttributeError, ValueError) as error:
        raise InvalidArtifact('Capture retry record is invalid or sealed with another key') from error
    if not re.fullmatch('[0-9a-f]{32}', str(identity)):
        raise InvalidArtifact('Capture retry record is invalid or sealed with another key')
    if not same_binding(job, capture_binding(config_root, data_root, media_root, directory, retire_to)):
        raise InvalidArtifact('Capture source, destination disk or retirement target changed; '
                              'capture into a new directory')
    if read_library_identity(data_root) != job.get('library_id'):
        raise InvalidArtifact('This capture belongs to another library')
    # The journal has no authentication, so it may only ever refuse a retry.
    phase = None
    if (directory / 'journal.json').exists():
        recorded = json.loads(read_bounded(directory / 'journal.json', 4096))
        if recorded.get('id') != identity:
            raise InvalidArtifact('Capture journal belongs to another capture')
        phase = recorded.get('phase')
    complete = (directory / 'library/complete.fernet').exists()
    if complete:
        # A damaged index raises here and stays in place for review.
        verify_library(directory, key)
    if complete or phase == 'complete':
        raise FileExistsError('This capture already finished; check it with verify-library')
    return job


def set_aside_unfinished(directory, key=None):
    """Keep old database snapshots, but retain resumable state under catalog/state."""
    from .resumable_state import HEADER, inspect
    inventory, copy = directory / 'inventory', directory / 'media-copy'
    unfinished = [directory / 'catalog', directory / 'library', directory / THUMBNAILS]
    catalog = directory / 'catalog'
    if key is not None and (catalog / 'state' / HEADER).exists():
        directory_identity(catalog)
        inspect(catalog / 'state', key)
        entries = list(catalog.iterdir())
        if any(entry.name not in {'state', 'database', 'catalog', 'journal.json', 'journal.part'}
               or entry.is_symlink() or entry.is_junction() for entry in entries):
            raise InvalidArtifact('Unsafe interrupted catalog entries')
        previous = directory / f'previous-catalog-{uuid.uuid4().hex}'
        previous.mkdir(mode=0o700)
        for entry in entries:
            if entry.name != 'state':
                entry.rename(previous / entry.name)
        sync_directory(previous)
        sync_directory(catalog)
        unfinished.remove(catalog)
    if inventory.exists() and ((inventory / 'INVENTORY-INCOMPLETE').exists()
                               or not (inventory / 'inventory.sqlite3').exists()):
        unfinished.append(inventory)
    if copy.exists() and not (copy / 'copy-job.json').exists():
        unfinished.append(copy)
    for path in unfinished:
        if path.exists():
            path.rename(directory / f'previous-{path.name}-{uuid.uuid4().hex}')
    sync_directory(directory)


CATALOG_FILES = ("SELECT m.relative_path,m.size,coalesce(m.stored_sha256,m.sha256),"
                 "m.original_relative_path,m.original_size,m.sha256 FROM media m "
                 "LEFT JOIN media_trash t ON t.media_id=m.id WHERE t.deleted_at IS NULL")


def catalog_digests(db, media_root, *, assume_immutable_source=False):
    """For a one-pass capture: the catalog's own size and SHA-256 of each file it lists.

    Used in place of reading the whole library once just to hash it. The copy must
    subsequently be checked against these digests, and reconcile_media compares the
    same inventory with the catalog. A file the catalog doesn't list, or lists at
    another size, is hashed here and now. Only Experimental opts out of source
    mutation checks while hashing, under the operator's immutable-source contract.
    """
    known = {}
    with db.cursor(name='backup_catalog_digests') as rows:
        rows.itersize = 1000
        rows.execute(CATALOG_FILES)
        for path, size, digest, original, original_size, original_digest in rows:
            known[unicodedata.normalize('NFC', path)] = (size, digest)
            if original and original != path:
                known.setdefault(unicodedata.normalize('NFC', original), (original_size, original_digest))

    def digest_of(name, info):
        listed = known.get(unicodedata.normalize('NFC', name))
        if (listed is not None and listed[0] == info.st_size and isinstance(listed[1], str)
                and re.fullmatch('[0-9a-f]{64}', listed[1])):
            return listed[1]
        if assume_immutable_source:
            return hash_file(media_root / name, signature(info), check_changes=False)
        return hash_file(media_root / name, signature(info))
    return digest_of


def reverify_inventory(inventory, media_root, progress, rehash=True):
    """Reread every source file. The earlier run's hashes are not trusted on their own.
    A one-pass retry passes `rehash=False`: the copy hashes each file it still has to copy."""
    try:
        return verify_media_inventory(inventory, media_root, rehash=rehash, progress=progress)
    except InvalidMediaInventory as error:
        raise InvalidMediaInventory('Media changed since the interrupted capture. Capture into a new directory.') from error


def capture_library(endpoint, pg_dump, config_root, data_root, media_root, directory, key, *,
                    database_budget, offline_confirmed=False, guard_factory=SpaceGuard, progress=None,
                    retire_to=None, resume=False, allow_unprotected=False, single_pass=False):
    """Capture one recovery point. `resume` retries the same interrupted capture.

    A retry verifies saved media and application-data blocks before reusing them.
    It always takes a fresh database dump under a new database freeze.
    `allow_unprotected` accepts a disk where anyone can read the plain media and names.

    `single_pass` reads the library once instead of three times. The inventory takes each
    file's SHA-256 from the catalog, the copy hashes every source file against it as it
    reads, and the copies are flushed to disk but not read back. The result is marked
    `media_read_back: False`: it is a backup only once verify-library, or the restore,
    has read every copy against the inventory. A retirement always reads them here.

    Wall seconds per phase, and per kind of work in the copy, go to `capture-timing.json`.
    """
    if offline_confirmed is not True:
        raise MaintenanceBlocked('Offline confirmation is required for whole-library capture')
    if not isinstance(key, BackupKey):
        raise TypeError('A library backup needs a key made from the administrator password')
    Fernet(key)
    if retire_to is not None:
        successor_id(retire_to)
    config_root, data_root, media_root, directory = (
        Path(os.path.abspath(p)) for p in (config_root, data_root, media_root, directory))
    for root in (config_root, data_root, media_root):
        if root.is_relative_to(directory) or directory.is_relative_to(root):
            raise ValueError('Backup must be separate from all source roots')
    if directory.exists() and not resume:
        raise FileExistsError('Whole-library capture requires a new directory')
    check_destination_privacy(directory.parent, allow_unprotected=allow_unprotected)
    identity = (resumable_capture(config_root, data_root, media_root, directory, key, retire_to)['id']
                if resume else uuid.uuid4().hex)
    timer = Timing(single_pass=single_pass, resumed=resume)
    with maintenance(data_root, 'backup') as admission_id:
        with freeze_database(endpoint) as db:
            library_id = ensure_library_identity(data_root, admission_id)
            reconcile_uploads(db, data_root)
            records = state_inventory({'config': config_root, 'data': data_root})
            state_budget = sum(row['size'] for row in records) + len(records) * 16384 + 10240
            from .resumable_state import HEADER, remaining_budget
            state_path = directory / 'catalog/state'
            resumable_state = resume and (state_path / HEADER).exists()
            state_allocation = (remaining_budget(state_path, key, state_budget) if resumable_state
                                else storage_budget(state_budget) + 2 * 65536)
            catalog_budget = (storage_budget(database_budget) + state_allocation + storage_budget(65536) * 2
                              + storage_budget(thumbnail_budget(data_root)))
            guard_factory([Requirement('backup', directory.parent, catalog_budget)]).check()
            if resume:
                set_aside_unfinished(directory, key)
            else:
                create_private_directory(directory, allow_unprotected=allow_unprotected)
                # Durably, before anything sealed with this key is written.
                publish_password(directory, key)
                write_capture_job(directory, key,
                                  capture_binding(config_root, data_root, media_root, directory, retire_to),
                                  identity, library_id)
            journal(directory, identity, 'media-inventory')
            timer.path = directory / TIMING
            with timer.phase('media-inventory'):
                if (directory / 'inventory').exists():
                    inventory = reverify_inventory(directory / 'inventory', media_root, progress,
                                                   rehash=not single_pass)
                else:
                    inventory = capture_media_inventory(
                        media_root, directory / 'inventory', offline_confirmed=True, guard_factory=guard_factory,
                        progress=progress, allow_unprotected=allow_unprotected,
                        known_digest=catalog_digests(db, media_root) if single_pass else None)
            timer.save(media_files=inventory['files'], media_bytes=inventory['bytes'])
            with timer.phase('reconcile'):
                counts = reconcile_media(db, directory / 'inventory')
            # Before the database dump and media copy, which can take many hours.
            check_copy_paths(directory / 'inventory/inventory.sqlite3', directory / 'media-copy')
            overhead = (directory / 'inventory/inventory.sqlite3').stat().st_size * 3 + inventory['files'] * 32768
            with closing(sqlite3.connect((directory / 'inventory/inventory.sqlite3').as_uri() + '?mode=ro', uri=True)) as files:
                files.execute('PRAGMA trusted_schema=OFF')
                remaining = remaining_media_bytes(files, directory / 'media-copy')
            guard_factory([Requirement('backup', directory, catalog_budget + remaining + overhead)]).check()
            with timer.phase('catalog'):
                components = capture_components(endpoint, pg_dump, config_root, data_root, directory / 'catalog', key,
                                                 identity, database_budget=database_budget,
                                                 guard_factory=guard_factory, progress=progress,
                                                 resume_state=resumable_state)
            # A few gigabytes that would take hours to rebuild from the media after a restore.
            journal(directory, identity, 'thumbnails')
            with timer.phase('thumbnails'):
                thumbnails = capture_thumbnails(data_root, directory / THUMBNAILS, key,
                                                guard_factory=guard_factory, progress=progress)
            journal(directory, identity, 'media-copy')
            with timer.phase('media-copy'):
                media = copy_media_tree(media_root, directory / 'inventory', directory / 'media-copy',
                                        offline_confirmed=True, guard_factory=guard_factory, progress=progress,
                                        allow_unprotected=allow_unprotected, read_back=not single_pass,
                                        timer=timer)
            with timer.phase('reconcile'):
                reconcile_media(db, directory / 'inventory')
            reconcile_uploads(db, data_root)
        catalog = publish_catalog(directory / 'catalog', key, identity, components,
                                  guard_factory=guard_factory, library_id=library_id)
        payload = publish_library_index(directory, key, identity, library_id, catalog, media, counts,
                                        thumbnails, guard_factory=guard_factory, retire_to=retire_to)
        if retire_to is not None:
            # Admission is still held: no participating writer can change the
            # just-captured source or restart between backup and retirement.
            with timer.phase('verify'):
                verify_library(directory, key)
            journal(directory, identity, 'retiring-source')
            retirement = mark_source_retired(data_root, admission_id, payload)
            publish_retirement(directory, key, retirement, guard_factory=guard_factory)
        journal(directory, identity, 'complete')
    return payload


def verify_library(directory, key, progress=None, *, media_result=None):
    """`media_result`, a dict, receives what the full read of the media found."""
    payload = verify_library_metadata(directory, key)
    media = verify_media_tree(media_copy_of(directory), progress)
    if media_result is not None:
        media_result.update(media)
    if media['files'] != payload.get('media_files') or media['bytes'] != payload.get('media_bytes'):
        raise InvalidArtifact('Media totals differ from authenticated recovery point')
    return payload


def verify_library_thumbnails(directory, key, payload):
    """Read and authenticate the backup's thumbnail copy, if it has one.

    Kept out of verify_library on purpose. Thumbnails are a cache that can be rebuilt, so a
    damaged copy must fail a check of the backup but never stop its photos and catalog
    from being restored. Only an explicit check of the backup calls this.
    """
    if payload.get('thumbnails') is None:
        return False
    verify_thumbnails(Path(directory) / THUMBNAILS, key, payload['thumbnails'])
    return True


def thumbnails_state(directory, key, payload):
    """'none', 'verified' or 'damaged', for a report. Never raises over the copy itself."""
    try:
        return 'verified' if verify_library_thumbnails(directory, key, payload) else 'none'
    except (InvalidArtifact, OSError):
        return 'damaged'


def verify_library_preflight(directory, key):
    """Sealed inputs only, never evidence that media is currently intact."""
    return verify_library_metadata(directory, key)


def verify_library_metadata(directory, key):
    """Authenticate the complete library recovery index."""
    directory = Path(directory)
    manifest = verify_artifact(directory / 'library', key)
    if manifest['metadata'].get('kind') != FORMAT or manifest['size'] > 65536:
        raise InvalidArtifact('Unsupported library recovery index')
    payload = json.loads(b''.join(read_chunks(directory / 'library', key, manifest)))
    if (not isinstance(payload, dict) or payload.get('format') != FORMAT
            or payload.get('activation_allowed') is not False or payload.get('media_encrypted') is not False):
        raise InvalidArtifact('Invalid library recovery index')
    validate_context_binding(key, payload.get('password'))
    if 'migration_target' in payload:
        successor_id(payload['migration_target'])
    catalog = verify_catalog(directory / 'catalog', key)
    if reference(catalog) != payload.get('catalog') or catalog['id'] != payload.get('id'):
        raise InvalidArtifact('Catalog belongs to another recovery point')
    if catalog.get('library_id') != payload.get('library_id'):
        raise InvalidArtifact('Catalog and media have different library identities')
    inventory = media_copy_of(directory) / 'inventory/inventory.sqlite3'
    if file_digest(inventory) != payload.get('media_inventory_sha256'):
        raise InvalidArtifact('Media inventory belongs to another recovery point')
    return payload
