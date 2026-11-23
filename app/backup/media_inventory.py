"""Disk-backed inventory of every physical file under an explicitly offline media root.

This is plain-media metadata, not a complete backup. Keep it private: names can be
sensitive. Callers must hold capture admission and reconcile catalog references.
No extension-based exclusions: retained originals, trash and staging all matter.
"""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import re
import unicodedata

from .private_storage import create_private_directory
from .sidecars import is_sidecar
from .space import Requirement, SpaceGuard, StorageUnavailable, directory_identity
from .volumes import volume_identity


class InvalidMediaInventory(ValueError):
    pass


def portable_path(name):
    if not isinstance(name, str) or len(name) > 4096 or any(c in name for c in '\\:<>"|?*'):
        raise InvalidMediaInventory('Media path is not portable between Windows and macOS')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10)),
                *(f'{prefix}{digit}' for prefix in ('COM', 'LPT') for digit in '\u00b9\u00b2\u00b3')}
    for part in name.split('/'):
        if (part in {'', '.', '..'} or part.endswith((' ', '.'))
                or any(ord(c) < 32 for c in part) or part.split('.')[0].rstrip(' ').upper() in reserved):
            raise InvalidMediaInventory('Unsafe media path component')
    return unicodedata.normalize('NFC', name).casefold()


def resolved_path(root, name, listings=None):
    """`root / name` with each part spelled as this system lists it.

    Windows treats a composed and a decomposed name as two names, so a name recorded on
    a Mac may be stored in the other form. An existing part keeps its stored spelling,
    and a part not on the disk yet keeps the inventory's. `listings` caches directory
    listings across a long copy.
    """
    path = Path(root) / name
    if name.isascii() or os.path.lexists(path):
        return path
    current, parts = Path(root), name.split('/')
    for index, part in enumerate(parts):
        candidate = current / part
        if not os.path.lexists(candidate):
            found = equivalent_entry(current, part, listings)
            if found is None:
                return current.joinpath(*parts[index:])
            candidate = current / found
        current = candidate
    return current


def equivalent_entry(directory, part, listings):
    """The one entry of a folder that differs from `part` only in Unicode form."""
    if listings is not None and directory in listings:
        names = listings[directory]
    else:
        try:
            names = os.listdir(directory)
        except OSError:
            names = []
        if listings is not None:
            listings[directory] = names
    composed = unicodedata.normalize('NFC', part)
    matches = [entry for entry in names if unicodedata.normalize('NFC', entry) == composed]
    return matches[0] if len(matches) == 1 else None


def matched_once(seen, stored):
    """One inventory entry may answer only one listed name.

    Where both spellings can exist as separate files, a second spelling of one file must
    not stand in for a different file that is missing. Only names with non-ASCII
    characters can have a twin, so only those are remembered.
    """
    if stored.isascii():
        return
    if stored in seen:
        raise InvalidMediaInventory('Two entries match one inventory path')
    seen.add(stored)


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def recorded_name(db, name):
    """The inventory's own spelling of a listed name, or None.

    macOS exFAT stores a name composed (NFC) but lists it decomposed, while Windows lists
    it as stored. So the same file can be listed in either Unicode form, and the match is
    by path first, then by form. Case never matches: only the Unicode form is forgiven.
    """
    if db.execute('SELECT 1 FROM entries WHERE path=?', (name,)).fetchone():
        return name
    composed = unicodedata.normalize('NFC', name)
    for (path,) in db.execute('SELECT path FROM entries WHERE portable=?', (portable_path(name),)):
        if unicodedata.normalize('NFC', path) == composed:
            return path
    return None


def walk(root):
    # scandir streams entries; the stack holds only directory depth, not a library.
    directory_identity(root)
    stack = [os.scandir(root)]
    try:
        while stack:
            entry = next(stack[-1], None)
            if entry is None:
                stack.pop().close()
                continue
            path = Path(entry.path)
            if path.is_symlink() or path.is_junction():
                raise InvalidMediaInventory('Linked media entries are not supported')
            # Windows DirEntry.stat can omit device/inode; Path.stat supplies
            # the identity required to compare with the subsequently opened file.
            info = path.stat(follow_symlinks=False)
            name = path.relative_to(root).as_posix()
            portable_path(name)
            if stat.S_ISDIR(info.st_mode):
                yield name, 'directory', info
                directory_identity(path)
                stack.append(os.scandir(path))
            elif stat.S_ISREG(info.st_mode):
                yield name, 'file', info
            else:
                raise InvalidMediaInventory('Non-regular media entry')
    finally:
        for iterator in stack:
            iterator.close()


def hash_file(path, expected, *, check_changes=True):
    directory_identity(path.parent)
    if path.is_symlink() or path.is_junction():
        raise InvalidMediaInventory('Linked media file')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (check_changes and signature(before) != expected):
            raise InvalidMediaInventory('Media changed before inventory hashing')
        while block := stream.read(8 * 1024 ** 2):
            digest.update(block)
        if check_changes and signature(os.fstat(stream.fileno())) != expected:
            raise InvalidMediaInventory('Media changed during inventory hashing')
    if check_changes and signature(path.stat(follow_symlinks=False)) != expected:
        raise InvalidMediaInventory('Media replaced during inventory hashing')
    return digest.hexdigest()


def recorded_volume(root):
    return json.dumps(volume_identity(root), sort_keys=True)


def validate_tree(root, db, *, rehash=False, progress=None, remounted=None, same_volume=None, sidecars_ignored=False):
    """Reconcile every directory and file; optional full source-content reread.

    `remounted` is a proven (old, new) device pair for this root's own volume. Anything
    recorded on another device, like a nested mount, must still match exactly.
    `same_volume` answers whether this is still the recorded disk. macOS exFAT gives an
    empty file a new inode when it remounts, and an empty file has no content to swap.
    """
    seen = 0
    twins = set()
    for name, kind, info in walk(root):
        stored = recorded_name(db, name)
        row = None if stored is None else db.execute(
            'SELECT kind, device, inode, size, mtime, sha256 FROM entries WHERE path=?', (stored,)).fetchone()
        if row is None and sidecars_ignored and is_sidecar(root / name):
            continue
        if row is None or row[0] != kind:
            raise InvalidMediaInventory('Media inventory changed')
        matched_once(twins, stored)
        expected = tuple(int(value) for value in row[1:5])
        if remounted and row[1] == remounted[0]:
            expected = (int(remounted[1]), *expected[1:])
        if (kind == 'file' and same_volume and expected[2] == 0 and info.st_size == 0
                and expected[1] != info.st_ino and same_volume()):
            expected = (expected[0], info.st_ino, *expected[2:])
        if kind == 'file':
            if signature(info) != expected:
                raise InvalidMediaInventory('Media inventory changed')
            if rehash and hash_file(root / name, expected) != row[5]:
                raise InvalidMediaInventory('Media content changed')
        elif (info.st_dev, info.st_ino) != expected[:2]:
            raise InvalidMediaInventory('Media directory replaced')
        seen += 1
        if progress:
            progress({'phase': 'inventory-verify', 'entries': seen})
    if seen != db.execute('SELECT count(*) FROM entries').fetchone()[0]:
        raise InvalidMediaInventory('Media inventory entries disappeared')


def capture_media_inventory(root, directory, *, offline_confirmed=False,
                            guard_factory=SpaceGuard, progress=None, allow_unprotected=False, authenticated=None,
                            known_digest=None, assume_immutable_source=False):
    """Create a NEW private inventory directory; incomplete inventories never qualify.

    Reading and hashing are bounded-memory. SQLite indexes detect case/Unicode
    collisions on disk. Offline confirmation is an operator prerequisite, not a
    replacement for the coordinating application's write-admission lease.
    `authenticated` is the inventory of a backup being read back. Sidecars it does not
    list are left out, and anything it does list is kept, whatever it is called.
    `known_digest(name, info)` supplies digests the caller has already proven, in
    place of hashing, so the result then says nothing new about the files' content.
    It is called with the authenticated inventory's own spelling of the name.
    Experimental capture can explicitly assume an operator-owned immutable source;
    only that mode skips the second tree walk and hashing-time mutation checks.
    """
    if offline_confirmed is not True:
        raise ValueError('Media inventory requires offline capture admission')
    if type(assume_immutable_source) is not bool:
        raise ValueError('Explicit boolean immutable-source policy required')
    if assume_immutable_source and authenticated is not None:
        raise ValueError('Authenticated inventory capture requires source rechecks')
    root, directory = Path(os.path.abspath(root)), Path(os.path.abspath(directory))
    identity = directory_identity(root)
    if root.is_relative_to(directory) or directory.is_relative_to(root):
        raise ValueError('Inventory storage must be separate from media')
    guard = guard_factory([Requirement('inventory', directory.parent, 1024 ** 2)])
    guard.check()
    create_private_directory(directory, allow_unprotected=allow_unprotected)
    marker = directory / 'INVENTORY-INCOMPLETE'
    marker.touch()
    database = directory / 'inventory.sqlite3'
    files = total = 0
    # A root with no durable volume identity can still be captured. Its inventory is
    # just never accepted again after a remount.
    try:
        volume = [('volume', recorded_volume(root))]
    except StorageUnavailable:
        volume = []
    with closing(sqlite3.connect(database)) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA cache_size=-2048')
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('CREATE TABLE entries (path TEXT PRIMARY KEY, portable TEXT UNIQUE NOT NULL, '
                   'kind TEXT NOT NULL, device TEXT NOT NULL, inode TEXT NOT NULL, '
                   'size INTEGER NOT NULL, mtime INTEGER NOT NULL, sha256 TEXT)')
        db.executemany('INSERT INTO metadata VALUES (?,?)', [
            ('format', 'familyfoto-media-inventory-v1'), ('state', 'incomplete'),
            ('source_root', str(root)), ('device', str(identity[0])), ('inode', str(identity[1])), *volume,
            *([('sidecars', 'ignored')] if authenticated else [])])
        db.commit()
        known = sqlite3.connect(Path(authenticated).as_uri() + '?mode=ro', uri=True) if authenticated else None
        for name, kind, info in walk(root):
            recorded = recorded_name(known, name) if known else None
            if known and recorded is None and is_sidecar(root / name):
                continue
            guard.check(next_role='inventory', next_bytes=65536 + len(name.encode('utf-8')) * 4)
            digest = None
            if kind == 'file':
                if known_digest:
                    digest = known_digest(recorded or name, info)
                elif assume_immutable_source:
                    digest = hash_file(root / name, signature(info), check_changes=False)
                else:
                    digest = hash_file(root / name, signature(info))
            try:
                db.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)',
                           (recorded or name, portable_path(name), kind, str(info.st_dev), str(info.st_ino),
                            info.st_size, info.st_mtime_ns, digest))
            except sqlite3.IntegrityError as error:
                raise InvalidMediaInventory('Media paths collide on a destination platform') from error
            if kind == 'file':
                files += 1
                total += info.st_size
            if progress:
                progress({'phase': 'inventory', 'files': files, 'bytes': total})
        if known:
            known.close()
        if not assume_immutable_source:
            validate_tree(root, db, sidecars_ignored=bool(authenticated))
        if directory_identity(root) != identity:
            raise InvalidMediaInventory('Media root replaced')
        db.execute("UPDATE metadata SET value='complete' WHERE key='state'")
        db.commit()
    # The durable database state alone is insufficient if this marker survives.
    marker.unlink()
    from .media_copy import sync_directory
    sync_directory(directory)
    return {'files': files, 'bytes': total, 'database': str(database), 'complete': True}


def verify_media_inventory(directory, root, *, references=(), rehash=True, progress=None):
    """Verify an inventory against its original offline root and catalog paths.

    References are root-relative paths supplied by catalog reconciliation, never
    inferred from extensions. Content verification defaults on. A metadata-only
    result must not be used as proof of backup integrity or recovery readiness.
    """
    if type(rehash) is not bool:
        raise ValueError('Explicit boolean verification mode required')
    directory, root = Path(os.path.abspath(directory)), Path(os.path.abspath(root))
    directory_identity(directory)
    identity = directory_identity(root)
    if (directory / 'INVENTORY-INCOMPLETE').exists():
        raise InvalidMediaInventory('Media inventory is incomplete')
    database = directory / 'inventory.sqlite3'
    if database.is_symlink() or database.is_junction() or not database.is_file():
        raise InvalidMediaInventory('Missing regular media inventory database')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        metadata = dict(db.execute('SELECT key, value FROM metadata'))
        if (metadata.get('format') != 'familyfoto-media-inventory-v1'
                or metadata.get('state') != 'complete'):
            raise InvalidMediaInventory('Unsupported or incomplete media inventory')
        if metadata.get('source_root') != str(root) or metadata.get('inode') != str(identity[1]):
            raise InvalidMediaInventory('Media inventory belongs to another source root')
        remounted = None
        if metadata.get('device') != str(identity[0]):
            if metadata.get('volume') is None or metadata['volume'] != recorded_volume(root):
                raise InvalidMediaInventory('Media inventory belongs to another source root')
            remounted = (metadata['device'], str(identity[0]))
        files = total = entries = 0
        for name, normalized, kind, size, digest in db.execute(
                'SELECT path, portable, kind, size, sha256 FROM entries'):
            entries += 1
            if portable_path(name) != normalized or kind not in {'file', 'directory'}:
                raise InvalidMediaInventory('Invalid media inventory entry')
            if type(size) is not int or size < 0:
                raise InvalidMediaInventory('Invalid media inventory size')
            if kind == 'file':
                if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                    raise InvalidMediaInventory('Invalid media inventory digest')
                files += 1
                total += size
        checked = 0
        for reference in references:
            portable_path(reference)
            stored = recorded_name(db, reference)
            found = None if stored is None else db.execute("SELECT kind FROM entries WHERE path=?", (stored,)).fetchone()
            if found != ('file',):
                raise InvalidMediaInventory('Catalog references media absent from the physical inventory')
            checked += 1
        def same_volume():
            return metadata.get('volume') is not None and metadata['volume'] == recorded_volume(root)
        validate_tree(root, db, rehash=rehash, progress=progress, remounted=remounted, same_volume=same_volume,
                      sidecars_ignored=metadata.get('sidecars') == 'ignored')
        if directory_identity(root) != identity:
            raise InvalidMediaInventory('Media root replaced during verification')
    return {'files': files, 'bytes': total, 'entries': entries, 'content_verified': rehash is True,
            'catalog_references_checked': checked}
