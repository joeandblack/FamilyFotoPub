"""Resumable offline plain-media tree copying. Never changes the active library."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil

from .media_copy import abandoned_claim, copy_lock, copy_media_file, publish_no_clobber, sync_directory
from .media_inventory import (hash_file, matched_once, recorded_name, resolved_path, signature, verify_media_inventory,
                              walk, InvalidMediaInventory)
from .media_paths import check_paths
from .private_storage import check_destination_privacy, create_private_directory
from .recovery import save_private, abandoned_publication
from .sidecars import is_sidecar
from .space import directory_identity, Requirement, SpaceGuard
from .timing import measured
from .volumes import same_except_device, volume_identity


def file_digest(path):
    directory_identity(path.parent)
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise InvalidMediaInventory('Expected regular copy metadata file')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_tree(db, tree, *, content=True, progress=None):
    """Reconcile the tree with its inventory. `content=False` checks names and sizes only.

    Returns a digest of every file's name, size and modification time, in no particular
    order. A later walk that gets the same digest has found no file added, removed,
    resized or rewritten since. It says nothing about content on its own.
    """
    total_files, total_bytes = db.execute(
        "SELECT count(*),coalesce(sum(size),0) FROM entries WHERE kind='file'").fetchone()
    count = files = checked_bytes = 0
    twins = set()
    seen = []
    for name, kind, info in walk(tree):
        stored = recorded_name(db, name)
        row = None if stored is None else db.execute(
            'SELECT kind, size, sha256 FROM entries WHERE path=?', (stored,)).fetchone()
        # A listed entry is always media. Only what the inventory does not list can be a sidecar.
        if row is None and is_sidecar(tree / name):
            continue
        if row is None or row[0] != kind:
            raise InvalidMediaInventory('Unexpected entry in copied media tree')
        matched_once(twins, stored)
        if kind == 'file':
            if info.st_size != row[1] or (content and hash_file(tree / name, signature(info)) != row[2]):
                raise InvalidMediaInventory('Copied media failed content verification')
            files += 1
            checked_bytes += info.st_size
            seen.append(f'{stored}\0{info.st_size}\0{info.st_mtime_ns}')
            if progress and content:
                progress({'phase': 'verify', 'files': files, 'total_files': total_files,
                          'bytes': checked_bytes, 'total_bytes': total_bytes})
        count += 1
    if count != db.execute('SELECT count(*) FROM entries').fetchone()[0]:
        raise InvalidMediaInventory('Copied media tree is incomplete')
    seen.sort()
    return hashlib.sha256('\n'.join(seen).encode('utf-8')).hexdigest()


def remove_finished_job(job):
    """A published file never needs its job folder again, and a large library would keep one
    per file. One left behind is harmless, since a retry finds the published file first."""
    try:
        for name in ('copy.json', 'copy.lock'):
            (job / name).unlink(missing_ok=True)
        job.rmdir()
    except OSError:
        pass


def check_copy_paths(inventory, target, progress=None):
    """Every inventoried entry and its per-file job must fit under this copy target."""
    target = Path(target)
    return check_paths(inventory, target / 'media', jobs=target / 'file-jobs', progress=progress)


def copy_media_tree(source, inventory, target, *, offline_confirmed=False,
                    guard_factory=SpaceGuard, progress=None, allow_unprotected=False,
                    read_back=True, timer=None):
    """Copy all inventoried files into target/media, preserving source and old backups.

    Target is a dedicated new job directory or a retry of that exact job. Interrupted
    files resume through verified checkpoints. Each file is read back before it is
    published, and completed files are rehashed on retry. Rereading the whole tree is
    left to verify_media_tree, which every recovery point runs before it counts.
    This does not rebind the application's disk configuration or activate recovery.

    With `read_back=False` each source file is still hashed against the inventory as it is
    read, but its copy is not read again here. The result says so (`content_verified` is
    False), and the tree is only proven by verify_media_tree, here or where it is restored.
    `timer` collects where the time went (app.backup.timing).
    """
    if offline_confirmed is not True:
        raise ValueError('Media tree copying requires offline capture admission')
    source, inventory, target = (Path(os.path.abspath(p)) for p in (source, inventory, target))
    for path in (source, inventory):
        if target.is_relative_to(path) or path.is_relative_to(target):
            raise ValueError('Copy target must be separate from source and inventory')
    def stage(phase):
        if not progress:
            return None
        progress({'phase': phase, 'entries': 0})
        return lambda event: progress({**event, 'phase': phase})

    summary = verify_media_inventory(inventory, source, rehash=False, progress=stage('copy-source-check'))
    check_copy_paths(inventory / 'inventory.sqlite3', target, progress=stage('copy-paths'))
    overhead = (inventory / 'inventory.sqlite3').stat().st_size * 2 + summary['entries'] * 16384 + 1024 ** 2
    expected = {'format': 'familyfoto-media-tree-v1', 'source': str(source),
                'source_identity': list(directory_identity(source)),
                'source_volume': volume_identity(source),
                'inventory_sha256': file_digest(inventory / 'inventory.sqlite3')}
    created = not target.exists()
    if created:
        guard_factory([Requirement('media', target.parent, summary['bytes'] + overhead)]).check()
        create_private_directory(target, allow_unprotected=allow_unprotected)
    else:
        # A retry must not continue on an unprotected disk without the same acceptance.
        check_destination_privacy(target.parent, allow_unprotected=allow_unprotected)
    directory_identity(target)
    with copy_lock(target):
        target_volume = volume_identity(target)
        marker = target / 'TREE-INCOMPLETE'
        record = target / 'copy-job.json'
        allowed = {'copy.lock', 'copy-job.json', 'copy-job.json.part', 'TREE-INCOMPLETE',
                   'TREE-COMPLETE.json', 'TREE-COMPLETE.json.part', 'media', 'file-jobs', 'inventory'}
        for entry in target.iterdir():
            if entry.name not in allowed and is_sidecar(entry, needs_sibling=False):
                continue
            if ((entry.name not in allowed and not abandoned_publication(entry, ('copy-job.json', 'TREE-COMPLETE.json')))
                    or entry.is_symlink() or entry.is_junction()):
                raise InvalidMediaInventory('Unowned copy directory; review required')
        if not record.exists():
            if not created or any(p.name != 'copy.lock' and not is_sidecar(p, needs_sibling=False)
                                  for p in target.iterdir()):
                raise InvalidMediaInventory('Unowned copy directory; review required')
            marker.touch()
            save_private(record, {**expected, 'target_identity': list(directory_identity(target)),
                                  'target_volume': target_volume})
        else:
            if record.is_symlink() or record.stat().st_size > 16384:
                raise InvalidMediaInventory('Invalid tree copy journal')
            previous = json.loads(record.read_text(encoding='utf-8'))
            if (not isinstance(previous, dict)
                    or any(previous.get(k) != v for k, v in expected.items() if k != 'source_identity')
                    or not same_except_device(previous.get('source_identity'), expected['source_identity'])
                    or not same_except_device(previous.get('target_identity'), directory_identity(target))
                    or previous.get('target_volume') != target_volume):
                raise InvalidMediaInventory('Copy source, inventory or destination changed')
            marker.touch(exist_ok=True)
        unfinished_report = target / 'TREE-COMPLETE.json.part'
        if unfinished_report.exists():
            if not unfinished_report.is_file():
                raise InvalidMediaInventory('Invalid interrupted completion report')
            unfinished_report.unlink()
        tree, jobs = target / 'media', target / 'file-jobs'
        snapshot = target / 'inventory'
        for directory in (tree, jobs, snapshot):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory_identity(directory)
        snapshot_file = snapshot / 'inventory.sqlite3'
        if not snapshot_file.exists():
            partial = snapshot / 'inventory.part'
            if partial.exists():
                if partial.is_symlink() or partial.is_junction() or not partial.is_file():
                    raise InvalidMediaInventory('Unsafe interrupted inventory copy')
                partial.unlink()
            with (inventory / 'inventory.sqlite3').open('rb') as incoming, partial.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 ** 2)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if file_digest(partial) != expected['inventory_sha256']:
                raise InvalidMediaInventory('Inventory changed while being copied')
            os.rename(partial, snapshot_file)
            sync_directory(snapshot)
        if file_digest(snapshot_file) != expected['inventory_sha256']:
            raise InvalidMediaInventory('Private inventory copy is incomplete or changed')
        verify_media_inventory(snapshot, source, rehash=False, progress=stage('copy-inventory-check'))
        with closing(sqlite3.connect(snapshot_file.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            capacity = guard_factory([Requirement('media', target, remaining_media_bytes(
                db, target, progress=stage('copy-space')) + overhead)])
            capacity.check()
            listings = {}
            source_listings = {}
            folder_progress = stage('copy-folders')
            folders = 0
            for (name,) in db.execute("SELECT path FROM entries WHERE kind='directory' ORDER BY length(path)"):
                folder = resolved_path(tree, name, listings)
                folder.mkdir(mode=0o700, exist_ok=True)
                directory_identity(folder)
                folders += 1
                if folder_progress:
                    folder_progress({'entries': folders})
            completed = copied_bytes = 0
            for name, size, digest in db.execute("SELECT path,size,sha256 FROM entries WHERE kind='file' ORDER BY path"):
                output = resolved_path(tree, name, listings)
                directory_identity(output.parent)
                payload = jobs / hashlib.sha256(name.encode('utf-8')).hexdigest() / 'payload'
                if abandoned_claim(output, payload):
                    output.unlink()
                    sync_directory(output.parent)
                if output.exists():
                    with measured(timer, 'recheck_copied'):
                        if output.is_symlink() or output.stat().st_size != size or file_digest(output) != digest:
                            raise InvalidMediaInventory('Existing copied file is damaged; review required')
                else:
                    job = jobs / hashlib.sha256(name.encode('utf-8')).hexdigest()
                    job.mkdir(mode=0o700, exist_ok=True)
                    directory_identity(job)
                    capacity.check()
                    allocated = allocated_bytes(job, size)
                    # The tree coordinator probes physical volumes once at admission
                    # and again before completion. Avoid diskutil per media file;
                    # per-file SpaceGuard still checks directory/device identities.
                    def pinned_volume(path):
                        if path.is_relative_to(source):
                            if directory_identity(path)[0] != expected['source_identity'][0]:
                                raise InvalidMediaInventory('Nested source volumes require separate copy roots')
                            return expected['source_volume']
                        if path.is_relative_to(target):
                            if directory_identity(path)[0] != directory_identity(target)[0]:
                                raise InvalidMediaInventory('Nested target volumes require separate copy roots')
                            return target_volume
                        raise InvalidMediaInventory('Copy path escaped its pinned volume roots')
                    payload = copy_media_file(resolved_path(source, name, source_listings), job, digest, guard_factory=guard_factory,
                                              volume_probe=pinned_volume, read_back=read_back, timer=timer)
                    # The source of this rename is our verified copy, never library media.
                    # Same-volume publication adds no second full-size temporary copy.
                    if output.exists():
                        raise InvalidMediaInventory('Media target appeared during copying')
                    with measured(timer, 'publish'):
                        publish_no_clobber(payload, output)
                        sync_directory(output.parent)
                        remove_finished_job(job)
                    capacity.written('media', max(0, size - allocated))
                completed += 1
                copied_bytes += size
                if progress:
                    progress({'phase': 'copy', 'files': completed, 'total_files': summary['files'],
                              'bytes': copied_bytes, 'total_bytes': summary['bytes']})
            verify_tree(db, tree, content=False)
        # No complete marker until both the source inventory and copied tree agree.
        verify_media_inventory(inventory, source, rehash=False)
        if file_digest(inventory / 'inventory.sqlite3') != expected['inventory_sha256']:
            raise InvalidMediaInventory('Inventory changed while copying')
        if volume_identity(source) != expected['source_volume'] or volume_identity(target) != target_volume:
            raise InvalidMediaInventory('Copy volume changed before completion')
        result = {'format': expected['format'], 'inventory_sha256': expected['inventory_sha256'],
                  'files': summary['files'], 'bytes': summary['bytes'], 'content_verified': read_back is True,
                  'activation_allowed': False}
        save_private(target / 'TREE-COMPLETE.json', result)
        marker.unlink()
        sync_directory(target)
        return result


def remaining_media_bytes(db, target, progress=None):
    """Bytes this copy job still needs, after finished files and partial progress."""
    remaining, listings = 0, {}
    checked = 0
    for name, size in db.execute("SELECT path,size FROM entries WHERE kind='file'"):
        if not resolved_path(target / 'media', name, listings).exists():
            job = target / 'file-jobs' / hashlib.sha256(name.encode('utf-8')).hexdigest()
            remaining += max(0, size - allocated_bytes(job, size))
        checked += 1
        if progress:
            progress({'phase': 'copy-space', 'entries': checked})
    return remaining


def allocated_bytes(job, size):
    if not job.exists():
        return 0
    directory_identity(job)
    used = 0
    for name in ('payload', 'payload.part'):
        path = job / name
        if path.exists():
            if path.is_symlink() or path.is_junction() or not path.is_file():
                raise InvalidMediaInventory('Unsafe partial media copy')
            used += path.stat().st_size
    return min(size, used)


def verify_media_tree(target, progress=None, *, content=True):
    """Verify the plain copied tree without access to the original disk.

    Hashes detect damage, not deliberate tampering with both manifests and media.
    A successful result is media-only, never complete catalog recovery readiness.
    `content=False` checks everything but file content: the manifests, and every name
    and size. Only app.backup.media_receipt may use it, beside a receipt of a full read.
    """
    target = Path(os.path.abspath(target))
    directory_identity(target)
    lock = target / 'copy.lock'
    if not lock.is_file() or lock.is_symlink() or lock.is_junction():
        raise InvalidMediaInventory('Missing regular copy ownership lock')
    with copy_lock(target):
        if (target / 'TREE-INCOMPLETE').exists():
            raise InvalidMediaInventory('Media tree copy is incomplete')
        values = []
        for name in ('copy-job.json', 'TREE-COMPLETE.json'):
            path = target / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                raise InvalidMediaInventory('Missing or invalid copy manifest')
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('format') != 'familyfoto-media-tree-v1':
                raise InvalidMediaInventory('Invalid media tree format')
            values.append(value)
        job, complete = values
        snapshot = target / 'inventory/inventory.sqlite3'
        directory_identity(snapshot.parent)
        if not (file_digest(snapshot) == job.get('inventory_sha256') == complete.get('inventory_sha256')):
            raise InvalidMediaInventory('Copy inventory checksum mismatch')
        with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            metadata = dict(db.execute('SELECT key,value FROM metadata'))
            if metadata.get('state') != 'complete' or metadata.get('format') != 'familyfoto-media-inventory-v1':
                raise InvalidMediaInventory('Incomplete media inventory')
            signature = verify_tree(db, target / 'media', content=content, progress=progress)
            files, size = db.execute("SELECT count(*),coalesce(sum(size),0) FROM entries WHERE kind='file'").fetchone()
            if complete.get('files') != files or complete.get('bytes') != size:
                raise InvalidMediaInventory('Copy manifest totals do not match')
        return {'files': files, 'bytes': size, 'content_verified': content is True,
                'tree_signature': signature, 'inventory_sha256': complete['inventory_sha256'],
                'catalog_included': False, 'activation_allowed': False}
