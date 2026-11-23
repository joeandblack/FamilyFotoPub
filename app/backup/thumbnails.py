"""The library's thumbnails, video posters, face pictures and photo previews, in a backup.

They are only a cache: every one can be made again from the media. But making them again
for a large library takes hours of reading, and all of them together are a few gigabytes.
So a whole-library backup keeps a sealed copy, and a recovered library can put them back
once it is activated, before it rebuilds whatever is still missing.

The copy is one encrypted artifact beside the catalog, never part of it. A backup without
one is still a complete backup, and nothing that restores or validates a catalog reads it.
"""
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import uuid

from .diagnostics import ExplainedFailure
from .encrypted import InvalidArtifact, read_bounded, read_chunks, seal_stream, verify_artifact
from .space import Requirement, SpaceGuard, directory_identity


KIND = 'familyfoto-thumbnails-v1'
FOLDER = 'thumbnails'
# The data folders whose every image is rebuildable from media, and what they may hold.
IMAGE = r'[^/]+\.(?:jpg|jpeg|png|webp)'
ROOTS = (('thumbnails', rf'thumbnails/(?:[^/]+/)*{IMAGE}'),
         ('previews', rf'previews/(?:[^/]+/)*{IMAGE}'),
         ('people/thumbnails', rf'people/thumbnails/{IMAGE}'))
HEADER = struct.Struct('>HQ')
MAX_FILES = 5_000_000
MAX_FILE = 256 * 1024 ** 2


def cached_name(name):
    """Refuse anything but an image under one of the cache folders."""
    if (not isinstance(name, str) or len(name) > 1024 or '\\' in name or ':' in name
            or any(part in {'', '.', '..'} or part.endswith((' ', '.')) or any(ord(char) < 32 for char in part)
                   for part in name.split('/'))
            or not any(re.fullmatch(pattern, name) for _, pattern in ROOTS)):
        raise InvalidArtifact('Unexpected file in the thumbnail copy')
    return name


def cached_files(data_root, progress=None):
    """Every cached image as (name, size), in a stable order. Other files are left out."""
    data_root = Path(os.path.abspath(data_root))
    found = []
    if progress:
        progress({'phase': 'thumbnails-inventory', 'entries': 0})
    for folder, pattern in ROOTS:
        top = data_root / folder
        if top.is_symlink() or not top.is_dir():
            continue
        pending = [top]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                        continue
                    name = Path(entry.path).relative_to(data_root).as_posix()
                    if stat.S_ISREG(info.st_mode) and re.fullmatch(pattern, name) and info.st_size <= MAX_FILE:
                        found.append((name, info.st_size))
                        if progress:
                            progress({'phase': 'thumbnails-inventory', 'entries': len(found)})
    return sorted(found)


class Frames(io.RawIOBase):
    """The files as one stream: a name length, a size, the name, then the bytes, for each."""
    def __init__(self, data_root, files):
        self.parts = self.produce(Path(data_root), files)
        self.pending = memoryview(b'')

    def produce(self, data_root, files):
        for name, size in files:
            encoded = name.encode('utf-8')
            yield HEADER.pack(len(encoded), size) + encoded
            sent = 0
            with (data_root / name).open('rb') as source:
                while block := source.read(1024 ** 2):
                    sent += len(block)
                    yield block
            if sent != size:
                raise InvalidArtifact('A thumbnail changed while it was being copied')

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            part = next(self.parts, None)
            if part is None:
                return 0
            self.pending = memoryview(part)
        size = min(len(buffer), len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size


def thumbnail_budget(data_root):
    """Plain bytes the sealed copy will hold."""
    files = cached_files(data_root)
    return sum(size for _, size in files) + len(files) * (HEADER.size + 1024)


def capture_thumbnails(data_root, directory, key, *, guard_factory=SpaceGuard, progress=None):
    """Seal every cached image into the NEW folder `directory`. None when there are none.

    Only for an offline capture: nothing may be writing thumbnails meanwhile.
    """
    data_root = Path(os.path.abspath(data_root))
    directory_identity(data_root)
    files = cached_files(data_root, progress=progress)
    if not files:
        return None
    if len(files) > MAX_FILES:
        raise InvalidArtifact('Too many thumbnails for one copy')
    total = sum(size for _, size in files)
    estimated = total + len(files) * (HEADER.size + 1024)
    report = (lambda count: progress({'phase': 'thumbnails', 'bytes': count})) if progress else None
    checked = (lambda count: progress({'phase': 'thumbnails-verify', 'bytes': count})) if progress else None
    if report:
        report(0)
    with io.BufferedReader(Frames(data_root, files), 1024 ** 2) as stream:
        return seal_stream(stream, directory, key, estimated_bytes=estimated, guard_factory=guard_factory,
                           progress=report, verify_progress=checked,
                           metadata={'kind': KIND, 'file_count': len(files), 'file_bytes': total})


class ChunkStream(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = memoryview(b'')

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.pending:
            self.pending = memoryview(next(self.chunks, b''))
        size = min(len(buffer), len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size


def exactly(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise InvalidArtifact('The thumbnail copy is cut short')
    return data


def walk_thumbnails(directory, key, manifest, consume):
    """Call consume(name, size, read) for each file. `read(n)` returns its next n bytes."""
    metadata = manifest['metadata']
    count, total = metadata.get('file_count'), metadata.get('file_bytes')
    if (metadata.get('kind') != KIND or type(count) is not int or not 1 <= count <= MAX_FILES
            or type(total) is not int or not 0 <= total <= manifest['size']):
        raise InvalidArtifact('Not a thumbnail copy')
    names = set()
    seen = 0
    with io.BufferedReader(ChunkStream(read_chunks(directory, key, manifest)), 1024 ** 2) as stream:
        for _ in range(count):
            length, size = HEADER.unpack(exactly(stream, HEADER.size))
            try:
                name = cached_name(exactly(stream, length).decode('utf-8'))
            except UnicodeDecodeError:
                raise InvalidArtifact('Unexpected file in the thumbnail copy') from None
            seen += size
            if name.casefold() in names or size > MAX_FILE or seen > total:
                raise InvalidArtifact('The thumbnail copy differs from its own summary')
            names.add(name.casefold())
            left = [size]

            def read(wanted, left=left):
                wanted = min(wanted, left[0])
                left[0] -= wanted
                return exactly(stream, wanted)
            consume(name, size, read)
            exactly(stream, left[0])
        # Exhaust the authenticated chunks: only then has the whole stream been checked.
        if stream.read(1) or seen != total:
            raise InvalidArtifact('The thumbnail copy differs from its own summary')
    return {'files': count, 'bytes': total}


def verify_thumbnails(directory, key, expected_reference):
    """The manifest of the backup's thumbnail copy, after reading and authenticating all of it."""
    from .catalog import reference
    manifest = verify_artifact(directory, key)
    if manifest['metadata'].get('kind') != KIND or reference(manifest) != expected_reference:
        raise InvalidArtifact('Thumbnail copy is missing, changed or from another recovery point')
    return manifest


def activated(target_directory):
    """Only an activated recovery may get its thumbnails back. Before that, validation
    requires its files to be exactly what the catalog restored."""
    try:
        settings = json.loads(read_bounded(Path(target_directory) / 'catalog/files/config/local.json', 1024 ** 2))
        recovery = settings['recovery']
        return recovery.get('state') == 'activated' and recovery.get('activation_required') is False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def remove_unfinished(data_root):
    """Drop what a restore that lost power left half written. Only this module makes these names."""
    for folder, _ in ROOTS:
        top = data_root / folder
        if top.is_dir() and not top.is_symlink():
            for path in top.rglob('*.restoring'):
                if re.fullmatch(r'.+\.[0-9a-f]{32}\.restoring', path.name) and path.is_file() and not path.is_symlink():
                    path.unlink()


def restore_thumbnails(backup, key, target_directory, *, guard_factory=SpaceGuard, progress=None):
    """Put the backup's cached images into an activated recovery. Never replaces a good file.

    A file already there with the right size is kept. Anything else is written new, so a
    run that was cut short can simply be repeated. A wrong-sized file that can't be
    replaced because the server has it open is counted as `in_use` and left for a later run. What the backup lacks is left for
    `launch --repair-thumbnails` to rebuild.
    """
    from .library import verify_library_metadata
    backup, target_directory = Path(os.path.abspath(backup)), Path(os.path.abspath(target_directory))
    payload = verify_library_metadata(backup, key)
    if not payload.get('thumbnails'):
        raise ExplainedFailure('This backup holds no thumbnails: it was made before backups kept them. '
                               'Rebuild them with launch --repair-thumbnails instead.')
    if not activated(target_directory):
        raise ExplainedFailure('Activate the recovered library first. Thumbnails are put back after activation.')
    data_root = target_directory / 'catalog/files/data'
    directory_identity(data_root)
    manifest = verify_thumbnails(backup / FOLDER, key, payload['thumbnails'])
    remove_unfinished(data_root)
    guard = guard_factory([Requirement('thumbnails', data_root, manifest['metadata']['file_bytes'])])
    guard.check()
    counts = {'restored': 0, 'kept': 0, 'in_use': 0}

    def write(name, size, read):
        path = data_root.joinpath(*name.split('/'))
        if path.is_symlink():
            raise InvalidArtifact('Linked thumbnail in the recovered library')
        if path.is_file() and path.stat().st_size == size:
            counts['kept'] += 1
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_identity(path.parent)
        # Written beside the final name and renamed, so a server already running never
        # serves half a picture. The name is one this module would never restore.
        partial = path.with_name(f'{path.name}.{uuid.uuid4().hex}.restoring')
        try:
            with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                remaining = size
                while remaining:
                    block = read(min(1024 ** 2, remaining))
                    output.write(block)
                    remaining -= len(block)
            try:
                os.replace(partial, path)
            except PermissionError:
                # Windows refuses to replace a file the running server has open. Only a
                # wrong-sized file gets here, so leave it, count it, and carry on.
                if not path.is_file():
                    raise
                counts['in_use'] += 1
                return
        finally:
            partial.unlink(missing_ok=True)
        counts['restored'] += 1
        if progress and counts['restored'] % 1000 == 0:
            progress({'phase': 'thumbnails', **counts})

    summary = walk_thumbnails(backup / FOLDER, key, manifest, write)
    return {'state': 'thumbnails-restored', **counts, 'files': summary['files'], 'bytes': summary['bytes']}
