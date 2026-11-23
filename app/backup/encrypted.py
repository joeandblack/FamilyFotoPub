"""Bounded authenticated artifacts using cryptography's standard Fernet tokens.

This versioned container binds each token to an artifact and position. The encrypted
completion manifest authenticates length, count and whole-stream SHA-256. It is not
a complete library recovery set. Keys are caller-managed and never stored here.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import uuid

from cryptography.fernet import Fernet, InvalidToken

from .media_copy import sync_directory
from .space import Requirement, SpaceGuard, byte_count, directory_identity


CHUNK_SIZE = 4 * 1024 ** 2
MANIFEST_LIMIT = 64 * 1024
FORMAT = "familyfoto-fernet-stream-v1"


class InvalidArtifact(ValueError):
    pass


def token_size(size):
    # Store the standard envelope in binary, avoiding base64's 33% disk overhead.
    return 57 + (size // 16 + 1) * 16


def encrypt_token(cipher, plain):
    return base64.urlsafe_b64decode(cipher.encrypt(plain))


def decrypt_token(cipher, token):
    return cipher.decrypt(base64.urlsafe_b64encode(token))


def storage_budget(size, chunk_size=CHUNK_SIZE):
    byte_count(size)
    if type(chunk_size) is not int or not 1 <= chunk_size <= CHUNK_SIZE:
        raise ValueError("Invalid encrypted chunk size")
    full, tail = divmod(size, chunk_size)
    return full * token_size(24 + chunk_size) + (token_size(24 + tail) if tail else 0) + MANIFEST_LIMIT


def read_bounded(path, limit):
    if path.is_symlink() or path.is_junction() or not stat.S_ISREG(path.stat().st_mode):
        raise InvalidArtifact("Invalid encrypted artifact entry")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise InvalidArtifact("Encrypted artifact entry exceeds its limit")
    return data


def validate_manifest(value):
    if not isinstance(value, dict) or set(value) != {"format", "id", "size", "count", "chunk_size", "sha256", "metadata"}:
        raise InvalidArtifact("Invalid artifact manifest")
    if value["format"] != FORMAT or not isinstance(value["id"], str) or not re.fullmatch("[0-9a-f]{32}", value["id"]):
        raise InvalidArtifact("Unsupported artifact identity or format")
    if not isinstance(value["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", value["sha256"]):
        raise InvalidArtifact("Invalid artifact digest")
    if type(value["size"]) is not int or not 0 <= value["size"] <= 4 * 1024 ** 4:
        raise InvalidArtifact("Invalid artifact size")
    if type(value["chunk_size"]) is not int or not 1 <= value["chunk_size"] <= CHUNK_SIZE:
        raise InvalidArtifact("Invalid artifact chunk size")
    if type(value["count"]) is not int or value["count"] != (value["size"] + value["chunk_size"] - 1) // value["chunk_size"]:
        raise InvalidArtifact("Invalid artifact chunk count")
    if not isinstance(value["metadata"], dict) or len(json.dumps(value["metadata"]).encode()) > 32 * 1024:
        raise InvalidArtifact("Invalid or oversized artifact metadata")
    return value


def load_manifest(directory, key):
    directory = Path(directory)
    directory_identity(directory)
    try:
        plain = decrypt_token(Fernet(key), read_bounded(directory / "complete.fernet", MANIFEST_LIMIT))
        return validate_manifest(json.loads(plain))
    except (InvalidToken, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidArtifact("Wrong recovery key or damaged encrypted manifest") from error


def read_chunks(directory, key, manifest):
    """Yield authenticated bytes; callers must exhaust this to validate the stream."""
    validate_manifest(manifest)
    cipher = Fernet(key)
    identity = bytes.fromhex(manifest["id"])
    digest = hashlib.sha256()
    size = 0
    for index in range(manifest["count"]):
        path = Path(directory) / f"{index:08d}.fernet"
        try:
            plain = decrypt_token(cipher, read_bounded(path, token_size(24 + manifest["chunk_size"])))
        except InvalidToken as error:
            raise InvalidArtifact("Wrong recovery key or damaged encrypted chunk") from error
        if plain[:24] != identity + struct.pack(">Q", index):
            raise InvalidArtifact("Encrypted chunks were reordered or belong to another artifact")
        block = plain[24:]
        expected = min(manifest["chunk_size"], manifest["size"] - size)
        if len(block) != expected:
            raise InvalidArtifact("Encrypted chunk length mismatch")
        digest.update(block)
        size += len(block)
        yield block
    if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
        raise InvalidArtifact("Encrypted stream verification failed")


def verify_artifact(directory, key):
    manifest = load_manifest(directory, key)
    for _ in read_chunks(directory, key, manifest):
        pass
    return manifest


def write_token(path, token, guard):
    guard.check(next_role="destination", next_bytes=len(token))
    with path.open("xb") as output:
        if output.write(token) != len(token):
            raise OSError("Short encrypted backup write")
        output.flush()
        os.fsync(output.fileno())
    guard.written("destination", len(token))


def seal_stream(stream, directory, key, *, estimated_bytes, chunk_size=CHUNK_SIZE,
                guard_factory=SpaceGuard, finish=None, progress=None, metadata=None, verify_progress=None):
    """Capture a NEW artifact; interrupted producers must restart into a new folder.

    `finish` must establish producer success before publication (e.g. pg_dump exit).
    No plaintext is written. Token timestamps and approximate sizes remain visible.
    """
    cipher = Fernet(key)  # Reject a bad key before creating anything.
    budget = storage_budget(estimated_bytes, chunk_size)
    directory = Path(os.path.abspath(directory))
    directory_identity(directory.parent)
    guard_factory([Requirement("destination", directory.parent, budget)]).check()
    directory.mkdir(mode=0o700)  # Never reuse/overwrite a previous artifact.
    guard = guard_factory([Requirement("destination", directory, budget)])
    identity = uuid.uuid4()
    size = count = 0
    digest = hashlib.sha256()
    while True:
        # Pipes may return short reads before EOF; keep chunks canonically sized.
        block = bytearray()
        while len(block) < chunk_size:
            part = stream.read(chunk_size - len(block))
            if not part:
                break
            block.extend(part)
        if not block:
            break
        token = encrypt_token(cipher, identity.bytes + struct.pack(">Q", count) + bytes(block))
        write_token(directory / f"{count:08d}.fernet", token, guard)
        size += len(block)
        count += 1
        digest.update(block)
        if progress:
            progress(size)
    if finish:
        finish()
    manifest = validate_manifest({"format": FORMAT, "id": identity.hex, "size": size, "count": count,
                                  "chunk_size": chunk_size, "sha256": digest.hexdigest(), "metadata": metadata or {}})
    verified = 0
    if verify_progress:
        verify_progress(verified)
    for block in read_chunks(directory, key, manifest):
        verified += len(block)
        if verify_progress:
            verify_progress(verified)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    token = encrypt_token(cipher, manifest_bytes)
    if len(token) > MANIFEST_LIMIT:
        raise InvalidArtifact("Encrypted manifest exceeds its limit")
    write_token(directory / "complete.part", token, guard)
    if decrypt_token(cipher, read_bounded(directory / "complete.part", MANIFEST_LIMIT)) != manifest_bytes:
        raise InvalidArtifact("Encrypted manifest failed read-back verification")
    guard.check()
    # Persist chunk entries before publishing the completion manifest.
    sync_directory(directory)
    os.rename(directory / "complete.part", directory / "complete.fernet")
    sync_directory(directory)
    return manifest
