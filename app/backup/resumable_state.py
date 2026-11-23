"""Resumable encrypted state stream; completed artifacts retain the v1 format.

The producer is replayed on retry. Reuse requires authenticated bytes equal to
the current stream, never just a checkpoint or matching file size/mtime.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import uuid

from cryptography.fernet import Fernet, InvalidToken

from .encrypted import (CHUNK_SIZE, MANIFEST_LIMIT, FORMAT, InvalidArtifact,
                        encrypt_token, decrypt_token, read_bounded, read_chunks,
                        storage_budget, token_size, validate_manifest, write_token)
from .media_copy import sync_directory
from .sidecars import is_sidecar
from .space import Requirement, SpaceGuard, directory_identity

HEADER = 'resume.fernet'
RESUME_FORMAT = 'familyfoto-state-resume-v1'
ENTRY = re.compile(r'(?:resume\.fernet|complete\.(?:part|fernet)|write\.part|[0-9]{8}\.fernet)')


def owned_sidecar(path):
    """On exFAT and other disks without extended attributes, macOS adds an AppleDouble `._name`
    next to each file written here. Its file may be gone: Windows, or any copy that skips
    sidecars, leaves it behind when this job replaces or removes that file. Only a validated
    sidecar for a name this folder owns is ignored."""
    return ENTRY.fullmatch(path.name[2:]) is not None and is_sidecar(path, needs_sibling=False)


def inspect_entries(directory):
    """Owned entries only; see owned_sidecar."""
    directory_identity(directory)
    entries = [path for path in directory.iterdir() if not owned_sidecar(path)]
    for path in entries:
        info = path.lstat()
        if (not ENTRY.fullmatch(path.name) or path.is_symlink() or path.is_junction()
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise InvalidArtifact('Unsafe application-data resume entry')
    return entries


def inspect(directory, key):
    entries = inspect_entries(directory)
    try:
        header = json.loads(decrypt_token(Fernet(key), read_bounded(directory / HEADER, MANIFEST_LIMIT)))
    except (InvalidToken, ValueError, UnicodeError) as error:
        raise InvalidArtifact('Wrong recovery key or damaged application-data resume record') from error
    if (not isinstance(header, dict) or set(header) != {'format', 'id', 'chunk_size'}
            or header['format'] != RESUME_FORMAT
            or not isinstance(header['id'], str) or not re.fullmatch('[0-9a-f]{32}', header['id'])
            or type(header['chunk_size']) is not int or not 1 <= header['chunk_size'] <= CHUNK_SIZE):
        raise InvalidArtifact('Invalid application-data resume record')
    return header, entries


def remaining_budget(directory, key, estimated_bytes):
    """Only existing allocation is credited; changed blocks need one scratch block.

    Content is authenticated and compared during streaming, before any reuse.
    Even corrupt blocks occupy storage that atomic replacement releases.
    """
    directory = Path(directory)
    if not directory.exists():
        return storage_budget(estimated_bytes) + MANIFEST_LIMIT * 2
    entries = inspect_entries(directory)
    if not (directory / HEADER).exists() and all(path.name == 'write.part' for path in entries):
        return storage_budget(estimated_bytes) + MANIFEST_LIMIT * 2
    header, entries = inspect(directory, key)
    max_count = (estimated_bytes + header['chunk_size'] - 1) // header['chunk_size']
    allocated = sum(min(path.stat().st_size, token_size(24 + header['chunk_size']))
                    for path in entries if re.fullmatch(r'[0-9]{8}\.fernet', path.name)
                    and int(path.stem) < max_count)
    return max(0, storage_budget(estimated_bytes, header['chunk_size']) - allocated) + token_size(
        24 + header['chunk_size']) + MANIFEST_LIMIT * 2


def replace_token(directory, name, token, guard):
    temporary = directory / 'write.part'
    guard.check()
    temporary.unlink(missing_ok=True)
    write_token(temporary, token, guard)
    if read_bounded(temporary, len(token)) != token:
        raise InvalidArtifact('Application-data block failed read-back verification')
    guard.check()
    os.replace(temporary, directory / name)
    sync_directory(directory)


def seal_state_stream(stream, directory, key, *, estimated_bytes, finish,
                      guard_factory=SpaceGuard, progress=None, metadata=None):
    directory = Path(directory)
    cipher = Fernet(key)
    budget = remaining_budget(directory, key, estimated_bytes)
    guard_factory([Requirement('destination', directory.parent, budget)]).check()
    if not (directory / HEADER).exists():
        directory.mkdir(mode=0o700, exist_ok=True)
        header = {'format': RESUME_FORMAT, 'id': uuid.uuid4().hex, 'chunk_size': CHUNK_SIZE}
        guard = guard_factory([Requirement('destination', directory, budget)])
        replace_token(directory, HEADER, encrypt_token(cipher, json.dumps(header).encode()), guard)
    else:
        header, _ = inspect(directory, key)
        guard = guard_factory([Requirement('destination', directory, budget)])
    # Completion must not remain visible while refreshing data against a new freeze.
    guard.check()
    (directory / 'complete.fernet').unlink(missing_ok=True)
    (directory / 'complete.part').unlink(missing_ok=True)
    sync_directory(directory)
    identity = bytes.fromhex(header['id'])
    chunk_size = header['chunk_size']
    digest = hashlib.sha256()
    count = size = 0
    while True:
        block = bytearray()
        while len(block) < chunk_size:
            part = stream.read(chunk_size - len(block))
            if not part:
                break
            block.extend(part)
        if not block:
            break
        guard.check()
        path = directory / f'{count:08d}.fernet'
        plain = identity + struct.pack('>Q', count) + bytes(block)
        reused = False
        if path.exists():
            try:
                reused = decrypt_token(cipher, read_bounded(path, token_size(24 + chunk_size))) == plain
            except (InvalidToken, InvalidArtifact):
                pass  # A torn or corrupt block is regenerated from the frozen source.
        if not reused:
            replace_token(directory, path.name, encrypt_token(cipher, plain), guard)
        digest.update(block)
        count += 1
        size += len(block)
        if progress:
            progress(size)
    finish()  # Source inventory/content verification and producer exit must succeed.
    # A smaller source stream may leave old tail blocks; only owned entries qualify.
    _, entries = inspect(directory, key)
    for path in entries:
        if (re.fullmatch(r'[0-9]{8}\.fernet', path.name) and int(path.stem) >= count) or path.name == 'write.part':
            guard.check()
            path.unlink()
    manifest = validate_manifest({'format': FORMAT, 'id': header['id'], 'size': size,
                                  'count': count, 'chunk_size': chunk_size,
                                  'sha256': digest.hexdigest(), 'metadata': metadata or {}})
    for _ in read_chunks(directory, key, manifest):
        guard.check()
    token = encrypt_token(cipher, json.dumps(manifest, sort_keys=True).encode())
    if len(token) > MANIFEST_LIMIT:
        raise InvalidArtifact('Application-data manifest exceeds its limit')
    replace_token(directory, 'complete.fernet', token, guard)
    return manifest
