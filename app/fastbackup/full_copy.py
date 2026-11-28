"""Non-resumable full media copy compatible with the existing verified tree reader."""
from contextlib import ExitStack, closing, contextmanager
import os
import hashlib
import stat
from pathlib import Path
import shutil
import sqlite3
import sys
import time

from app.backup.media_copy import copy_lock, sync_directory
from app.backup.media_inventory import (InvalidMediaInventory, portable_path, recorded_volume,
                                      resolved_path)
from app.backup.media_tree import file_digest, hash_in_order, verify_tree
from app.backup.private_storage import create_private_directory, check_destination_privacy
from app.backup.recovery import save_private
from app.backup.space import Requirement, SpaceGuard, directory_identity
from app.backup.volumes import volume_identity
from .native_copy import copy_file, flush_device, pin_directory

if sys.platform == 'win32':
    from .windows_bulk_copy import copy_file, copy_files


SPACE_CHECK_BYTES = 1024 ** 3
SPACE_CHECK_SECONDS = 30


class _CopyCapacity:
    """Admit the whole remaining copy upfront, then check between files periodically."""

    def __init__(self, guard, clock=time.monotonic):
        self.guard, self.clock = guard, clock
        self.bytes_since_check = 0
        self.checked_at = None

    def check(self):
        now = self.clock()
        if (self.checked_at is None or self.bytes_since_check >= SPACE_CHECK_BYTES
                or now - self.checked_at >= SPACE_CHECK_SECONDS):
            self.guard.check()
            self.checked_at = self.clock()
            self.bytes_since_check = 0

    def written(self, size):
        self.guard.written('media', size)
        self.bytes_since_check += size


def _flush_and_verify(path, size, digest):
    """Use an ordinary short-lived file handle, not a retained destination lock."""
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink() or path.is_junction()
            or getattr(before, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Unexpected destination type during verification')
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    with path.open('r+b') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before) or before.st_size != size:
            raise ValueError('Destination changed before verification')
        os.fsync(stream.fileno())
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise ValueError('Destination failed full verification')
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Destination changed during verification')
    if identity(path.lstat()) != identity(before):
        raise ValueError('Destination changed after verification')


def _inventory_summary(inventory, source):
    """Check the saved inventory's root binding, without walking source media again."""
    directory_identity(inventory)
    identity = directory_identity(source)
    if (inventory / 'INVENTORY-INCOMPLETE').exists():
        raise InvalidMediaInventory('Media inventory is incomplete')
    database = inventory / 'inventory.sqlite3'
    if database.is_symlink() or database.is_junction() or not database.is_file():
        raise InvalidMediaInventory('Missing regular media inventory database')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        metadata = dict(db.execute('SELECT key, value FROM metadata'))
        if metadata.get('format') != 'familyfoto-media-inventory-v1' or metadata.get('state') != 'complete':
            raise InvalidMediaInventory('Unsupported or incomplete media inventory')
        if metadata.get('source_root') != str(source) or metadata.get('inode') != str(identity[1]):
            raise InvalidMediaInventory('Media inventory belongs to another source root')
        if metadata.get('device') != str(identity[0]):
            if metadata.get('volume') is None or metadata['volume'] != recorded_volume(source):
                raise InvalidMediaInventory('Media inventory belongs to another source root')
        if db.execute("SELECT 1 FROM entries WHERE kind NOT IN ('file','directory') "
                      "OR typeof(size) != 'integer' OR size < 0 LIMIT 1").fetchone():
            raise InvalidMediaInventory('Invalid media inventory entry')
        if db.execute("SELECT 1 FROM entries WHERE kind='file' AND "
                      "(sha256 IS NULL OR typeof(sha256) != 'text' OR length(sha256) != 64 "
                      "OR sha256 GLOB '*[^0-9a-f]*') LIMIT 1").fetchone():
            raise InvalidMediaInventory('Invalid media inventory digest')
        entries = db.execute('SELECT count(*) FROM entries').fetchone()[0]
        files, total = db.execute("SELECT count(*),coalesce(sum(size),0) FROM entries WHERE kind='file'").fetchone()
    return dict(entries=entries, files=files, bytes=total)


@contextmanager
def _timed_phase(progress, phase, summary, **details):
    started = time.monotonic()
    counts = dict(files=0, bytes=0, total_files=summary['files'], total_bytes=summary['bytes'])

    def report(event=None, *, state='running'):
        if event:
            counts.update({key: event[key] for key in counts if key in event})
        if progress:
            progress(dict(counts, **details, phase=phase, state=state,
                          elapsed_seconds=round(time.monotonic() - started, 3)))

    report(state='started')
    try:
        yield report
    except BaseException:
        try:
            report(state='failed')
        except BaseException:
            pass  # A broken progress channel must not replace the original failure.
        raise
    else:
        report(state='completed')


def copy_media_tree(source, inventory, target, *, offline_confirmed=False,
                    allow_unprotected=False, progress=None, cancelled=lambda: False,
                    guard_factory=SpaceGuard):
    if offline_confirmed is not True:
        raise ValueError('Offline confirmation required')
    preparation_started = time.monotonic()
    if progress:
        progress(dict(phase='media-preparation', state='started', elapsed_seconds=0))
    source, inventory, target = [Path(os.path.abspath(p)) for p in (source, inventory, target)]
    for root in (source, inventory):
        if target.is_relative_to(root) or root.is_relative_to(target):
            raise ValueError('Copy target must be separate from sources')
    if os.path.lexists(target):
        raise FileExistsError('Retry requires a new destination; incomplete files are retained')
    check_destination_privacy(target.parent, allow_unprotected=allow_unprotected)
    summary = _inventory_summary(inventory, source)
    inventory_hash = file_digest(inventory / 'inventory.sqlite3')
    overhead = (inventory / 'inventory.sqlite3').stat().st_size * 2 + summary['entries'] * 16384 + 1024**2
    guard_factory([Requirement('media', target.parent, summary['bytes'] + overhead)]).check()
    source_volume = volume_identity(source)
    with ExitStack() as pins:
        # Retain ancestor identities before creating anything below them.
        for root in (source, inventory, target.parent):
            for parent in (*reversed(root.parents), root):
                if parent != Path(parent.anchor):
                    pin_directory(pins, parent)
        create_private_directory(target, allow_unprotected=allow_unprotected)
        pin_directory(pins, target)
        target_volume = volume_identity(target)
        with copy_lock(target):
            marker = target / 'TREE-INCOMPLETE'
            marker.touch(exist_ok=False)
            record = dict(format='familyfoto-media-tree-v1', source=str(source),
                          source_identity=list(directory_identity(source)), source_volume=source_volume,
                          target_identity=list(directory_identity(target)), target_volume=target_volume,
                          inventory_sha256=inventory_hash)
            save_private(target / 'copy-job.json', record)
            tree, snapshot = target / 'media', target / 'inventory'
            for folder in (tree, snapshot):
                folder.mkdir(mode=0o700)
                pin_directory(pins, folder)
            with (inventory / 'inventory.sqlite3').open('rb') as src, (snapshot / 'inventory.sqlite3').open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024**2)
                dst.flush()
                os.fsync(dst.fileno())
            if file_digest(snapshot / 'inventory.sqlite3') != inventory_hash:
                raise ValueError('Inventory changed during snapshot')
            capacity = _CopyCapacity(guard_factory([Requirement('media', target, summary['bytes'] + overhead)]))
            capacity.check()
            with closing(sqlite3.connect((snapshot / 'inventory.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('PRAGMA trusted_schema=OFF')
                db.execute('BEGIN')
                source_list, target_list = {}, {}
                prepared = {''}

                def prepare_folder(name):
                    # Validate names before resolving them. Create/pin each directory once,
                    # when its first file needs it, rather than in a separate tree pass.
                    if name in prepared:
                        return
                    portable_path(name)
                    current = ''
                    for part in name.split('/'):
                        current = current + '/' + part if current else part
                        if current in prepared:
                            continue
                        row = db.execute('SELECT kind,portable FROM entries WHERE path=?', (current,)).fetchone()
                        if row != ('directory', portable_path(current)):
                            raise InvalidMediaInventory('Missing or invalid inventory parent directory')
                        pin_directory(pins, resolved_path(source, current, source_list))
                        folder = resolved_path(tree, current, target_list)
                        folder.mkdir(mode=0o700)
                        pin_directory(pins, folder)
                        prepared.add(current)

                if progress:
                    progress(dict(phase='media-preparation', state='completed',
                                  elapsed_seconds=round(time.monotonic() - preparation_started, 3)))
                copied = count = 0
                with _timed_phase(progress, 'media-transfer', summary) as report:
                    def transferred(size):
                        nonlocal copied, count
                        capacity.written(size)
                        capacity.check()
                        copied += size
                        count += 1
                        report(dict(files=count, bytes=copied))

                    if sys.platform == 'win32':
                        # Validate inventory names in memory, not source files.
                        # No source walk, per-file stat, parent pin or stream scan.
                        entries = list(db.execute('SELECT path,portable,kind,size FROM entries'))
                        directories = {''}
                        files = []
                        for name, normalized, kind, size in entries:
                            if portable_path(name) != normalized:
                                raise InvalidMediaInventory('Invalid media inventory path')
                            if kind == 'directory':
                                directories.add(name)
                            else:
                                files.append((name, size))
                        for name, _, _, _ in entries:
                            if name.rpartition('/')[0] not in directories:
                                raise InvalidMediaInventory('Missing inventory parent directory')
                        for name in sorted(directories - {''}, key=len):
                            if cancelled():
                                raise InterruptedError('Copy cancelled')
                            (tree / name).mkdir(mode=0o700)
                        copy_files(source, tree, sorted(files), copier=copy_file,
                                   cancelled=cancelled, completed=transferred)
                    for name, normalized, size in (() if sys.platform == 'win32' else db.execute("SELECT path,portable,size FROM entries WHERE kind='file' ORDER BY path")):
                        if cancelled():
                            raise InterruptedError('Copy cancelled')
                        if portable_path(name) != normalized:
                            raise InvalidMediaInventory('Invalid media inventory path')
                        prepare_folder(name.rpartition('/')[0])
                        capacity.check()
                        incoming = resolved_path(source, name, source_list)
                        outgoing = resolved_path(tree, name, target_list)
                        copy_file(incoming, outgoing, cancelled=cancelled)
                        # The separate verification pass checks every destination size.
                        transferred(size)
                    # Preserve empty directories too; no separate source-file traversal.
                    for (name,) in (() if sys.platform == 'win32' else db.execute("SELECT path FROM entries WHERE kind='directory' ORDER BY length(path)")):
                        if cancelled():
                            raise InterruptedError('Copy cancelled')
                        prepare_folder(name)
                # The application remains offline. Destination files are not kept
                # locked across phases; verification uses short-lived ordinary handles.
                with _timed_phase(progress, 'media-verification', summary,
                                  verification='full') as report:
                    checked = checked_bytes = 0
                    hashed = None
                    if cancelled():
                        raise InterruptedError('Verification cancelled')
                    flush_device(snapshot / 'inventory.sqlite3')
                    if sys.platform == 'win32':
                        for name, size, digest in db.execute("SELECT path,size,sha256 FROM entries WHERE kind='file' ORDER BY path"):
                            if cancelled():
                                raise InterruptedError('Verification cancelled')
                            _flush_and_verify(resolved_path(tree, name, target_list), size,
                                              digest)
                            checked += 1
                            checked_bytes += size
                            report(dict(files=checked, bytes=checked_bytes))
                    else:
                        def checked_progress(event):
                            if cancelled():
                                raise InterruptedError('Verification cancelled')
                            report(event)
                        # Path order is the copy's write order; the final check's walk
                        # binds what was read to the tree and rules out extra entries.
                        hashed = hash_in_order(db, tree, progress=checked_progress)
                with _timed_phase(progress, 'media-final-check', summary) as report:
                    verify_tree(db, tree, content=False, hashed=hashed)
                    if (file_digest(inventory / 'inventory.sqlite3') != inventory_hash
                            or volume_identity(source) != source_volume or volume_identity(target) != target_volume):
                        raise ValueError('Inventory or volume changed before completion')
                    if cancelled():
                        raise InterruptedError('Copy cancelled before publication')
                    report(dict(files=count, bytes=copied))
            if cancelled():
                raise InterruptedError('Copy cancelled before publication')
            result = dict(format=record['format'], inventory_sha256=inventory_hash, files=count,
                          bytes=copied, content_verified=True, activation_allowed=False)
            save_private(target / 'TREE-COMPLETE.json', result)
            flush_device(target / 'TREE-COMPLETE.json')
            marker.unlink()
            sync_directory(target)
            return result
