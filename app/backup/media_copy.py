"""Resumable single-file plain-media copy primitive, not a catalog backup command.

Only call with stable media and a trusted expected SHA-256 from capture admission.
Never use this plaintext writer for credentials, database dumps or face state.
"""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re

from .sidecars import is_sidecar
from .timing import measured
from .space import Requirement, SpaceGuard, directory_identity
from .volumes import same_except_device, volume_identity


# What a disk without hard links, such as exFAT, answers to a link request.
NO_HARD_LINKS = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.ENOSYS}


class InvalidCopy(ValueError):
    pass


def publish_by_claim(source, target):
    """Publish without hard links, and without ever replacing another file.

    The exclusive create is what refuses an existing target, unlike checking first. It
    leaves an empty claim, which only ever gets replaced by this job's verified file. If
    this stops between the two steps, `abandoned_claim` lets the retry remove the claim.
    """
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    sync_directory(target.parent)
    if os.stat(target).st_size != 0:
        raise InvalidCopy('Publication target changed after it was claimed')
    os.replace(source, target)


def publish_no_clobber(source, target):
    """Move a verified file into place, refusing an existing target on every filesystem."""
    if os.name == 'nt':
        # Windows rename refuses an existing target.
        os.rename(source, target)
        return
    # POSIX rename would overwrite it, so use a hard link where the disk has them.
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno not in NO_HARD_LINKS:
            raise
        publish_by_claim(source, target)
    else:
        sync_directory(target.parent)
        source.unlink()


def abandoned_claim(target, unpublished):
    """An empty target, while the verified file meant to replace it still waits."""
    try:
        return (not target.is_symlink() and target.is_file() and target.stat().st_size == 0
                and unpublished.is_file() and unpublished.stat().st_size > 0)
    except OSError:
        return False


def sync_directory(path):
    # Windows does not expose fsync on directories via Python. File fsync plus
    # fail-closed recovery is still required; power-loss guarantees need OS drills.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def copy_lock(directory):
    path = directory / "copy.lock"
    if path.is_symlink() or path.is_junction():
        raise InvalidCopy("Linked copy lock")
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def save_checkpoint(directory, record):
    temporary = directory / "copy.json.tmp"
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(record, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, directory / "copy.json")
    sync_directory(directory)


def _without_inode(recorded, signature):
    return [recorded[0], signature[1], *recorded[2:]] if isinstance(recorded, list) and len(recorded) == 4 else recorded


def prefix_digest(handle, size, chunk_size):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = handle.read(min(chunk_size, remaining))
        if not block:
            raise InvalidCopy("Incomplete verified copy prefix")
        digest.update(block)
        remaining -= len(block)
    return digest


def copy_media_file(source, directory, expected_sha256, *, chunk_size=64 * 1024 ** 2,
                    guard_factory=SpaceGuard, progress=None, volume_probe=volume_identity,
                    read_back=True, timer=None):
    """Copy into an existing dedicated job directory; publish `payload` once verified.

    A retry rechecks the prefix and copies only remaining bytes. Checkpoints precede
    progress notifications. Exceptions leave state intact. One job owns this folder;
    arbitrary earlier backups are never overwritten or pruned.

    The source is always hashed as it is read and must match `expected_sha256`. With
    `read_back=False` the written copy is flushed to disk but not read again, so a caller
    must have every copy read against the inventory later, before it counts as a backup.
    `timer` collects where the time went (app.backup.timing).
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Expected SHA-256 is required")
    if type(chunk_size) is not int or not 1 <= chunk_size <= 64 * 1024 ** 2:
        raise ValueError("Bounded positive chunk size required")
    source = Path(os.path.abspath(source))
    directory = Path(os.path.abspath(directory))
    directory_identity(source.parent)
    identity = directory_identity(directory)
    source_volume = volume_probe(source.parent)
    target_volume = volume_probe(directory)
    if source.is_symlink() or not source.is_file() or source.is_relative_to(directory):
        raise InvalidCopy("Require an independent regular media source")
    allowed = {"copy.lock", "copy.json", "copy.json.tmp", "payload.part", "payload"}
    for entry in directory.iterdir():
        if entry.name not in allowed and is_sidecar(entry, needs_sibling=False):
            continue
        if entry.name not in allowed or entry.is_symlink() or not entry.is_file():
            raise InvalidCopy("Unexpected entry in copy job directory")
    with copy_lock(directory), source.open("rb") as incoming:
        info = os.fstat(incoming.fileno())
        signature = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        checkpoint = directory / "copy.json"
        part = directory / "payload.part"
        final = directory / "payload"
        expected = {"format": "familyfoto-media-copy-v1", "sha256": expected_sha256,
                    "source": signature, "directory": list(identity),
                    "source_volume": source_volume, "target_volume": target_volume}
        if checkpoint.exists():
            if checkpoint.stat().st_size > 8192:
                raise InvalidCopy("Oversized copy checkpoint")
            record = json.loads(checkpoint.read_text(encoding="utf-8"))
            # macOS exFAT gives an empty file a new inode on remount, and it has no content to swap.
            source_inode_counts = info.st_size != 0
            if (not isinstance(record, dict)
                    or any(record.get(key) != expected[key] for key in ("format", "sha256", "source_volume", "target_volume"))
                    or not same_except_device(record.get("source") if source_inode_counts
                                              else _without_inode(record.get("source"), signature), signature)
                    or not same_except_device(record.get("directory"), identity)):
                raise InvalidCopy("Copy source or destination changed; review before resuming")
        else:
            if part.exists() or final.exists():
                raise InvalidCopy("Unowned copy output; cannot resume")
            record = {**expected, "offset": 0, "prefix_sha256": hashlib.sha256().hexdigest()}
        offset = record.get("offset")
        if type(offset) is not int or not 0 <= offset <= info.st_size:
            raise InvalidCopy("Invalid copy checkpoint offset")
        guard = guard_factory([Requirement("destination", directory, info.st_size - offset)])
        guard.check()
        if abandoned_claim(final, part):
            final.unlink()
            sync_directory(directory)
        if final.exists():
            with final.open("rb") as output:
                if os.fstat(output.fileno()).st_size != info.st_size or prefix_digest(output, info.st_size, chunk_size).hexdigest() != expected_sha256:
                    raise InvalidCopy("Published copy failed verification")
            return final
        if not checkpoint.exists():
            with measured(timer, 'checkpoint'):
                save_checkpoint(directory, record)
        with part.open("r+b" if part.exists() else "x+b") as output:
            digest = prefix_digest(output, offset, chunk_size)
            if digest.hexdigest() != record.get("prefix_sha256"):
                raise InvalidCopy("Copy prefix failed verification; cannot resume")
            # Uncheckpointed tail after a crash belongs only to this unfinished copy.
            output.truncate(offset)
            output.seek(offset)
            incoming.seek(offset)
            while offset < info.st_size:
                with measured(timer, 'source_read'):
                    block = incoming.read(min(chunk_size, info.st_size - offset))
                if not block:
                    raise InvalidCopy("Source shortened while copying")
                guard.check(next_role="destination", next_bytes=len(block))
                with measured(timer, 'destination_write'):
                    if output.write(block) != len(block):
                        raise OSError("Short backup write")
                    output.flush()
                guard.written("destination", len(block))
                with measured(timer, 'hash'):
                    digest.update(block)
                offset += len(block)
                # Most media fits in one block, where a checkpoint would only record the end of
                # a file about to be published. A crash before that resumes from the last one.
                if offset < info.st_size:
                    with measured(timer, 'checkpoint'):
                        os.fsync(output.fileno())
                        record.update(offset=offset, prefix_sha256=digest.hexdigest())
                        save_checkpoint(directory, record)
                if progress:
                    progress(offset, info.st_size)
            after = os.fstat(incoming.fileno())
            if signature != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns]:
                raise InvalidCopy("Source changed while copying")
            if digest.hexdigest() != expected_sha256:
                raise InvalidCopy("Source content did not match expected SHA-256")
            with measured(timer, 'flush_to_disk'):
                output.flush()
                os.fsync(output.fileno())
            if read_back:
                with measured(timer, 'read_back'):
                    output.seek(0)
                    if prefix_digest(output, info.st_size, chunk_size).hexdigest() != expected_sha256:
                        raise InvalidCopy("Copied file failed read-back verification")
        guard.check()
        if volume_probe(source.parent) != source_volume or volume_probe(directory) != target_volume:
            raise InvalidCopy('Copy volume changed before publication')
        with measured(timer, 'publish'):
            publish_no_clobber(part, final)
            sync_directory(directory)
        # A retry that finds `payload` rereads it, so publication needs no record of its own.
        return final
