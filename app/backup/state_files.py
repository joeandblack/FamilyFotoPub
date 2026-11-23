"""Encrypted file-side state capture and isolated restoration, never activation.

Stable-file checks detect observed changes, but are NOT a replacement for the
application-wide writer admission gate needed to pair these files with a DB snapshot.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import threading
import unicodedata

from cryptography.fernet import Fernet

from .encrypted import read_chunks, verify_artifact
from .inventory import state_files
from .media_copy import sync_directory
from .policy import classify_file
from .private_storage import create_private_directory
from .space import Requirement, SpaceGuard, directory_identity
from .resumable_state import remaining_budget, seal_state_stream


MAX_FILES = 100_000
MAX_REQUEST = 32 * 1024 ** 2
STATE_KIND = "familyfoto-state-tar-v1"


class InvalidState(ValueError):
    pass


def safe_member(name):
    if not isinstance(name, str) or len(name) > 4096 or "\\" in name or ":" in name:
        raise InvalidState("Unsafe state archive path")
    parts = name.split("/")
    if len(parts) < 2 or parts[0] not in {"config", "data"}:
        raise InvalidState("Unknown state archive root")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in parts:
        if (part in {"", ".", ".."} or part.endswith((" ", ".")) or any(ord(char) < 32 for char in part)
                or part.split(".")[0].upper() in reserved):
            raise InvalidState("Unsafe state archive component")
    treatment = classify_file(parts[0], "/".join(parts[1:]))
    if treatment.category != "authoritative":
        raise InvalidState("Archive contains non-authoritative or runnable control state")
    return PurePosixPath(*parts)


def state_inventory(roots, *, on_unclassified=None):
    """Validate state; only preflight may collect unknown entries instead of raising.

    A caller supplying on_unclassified must reject the plan if it receives any
    entries. Capture callers leave it unset and continue to fail closed.
    """
    if set(roots) != {"config", "data"}:
        raise InvalidState("Explicit config and data roots required")
    records = []
    names = set()
    for kind, root in roots.items():
        directory_identity(root)
        for record in state_files(root, kind, **({'on_unclassified': on_unclassified} if on_unclassified else {})):
            if record["category"] != "authoritative":
                continue
            name = f"{kind}/{record['path']}"
            safe_member(name)
            normalized = unicodedata.normalize("NFC", name).casefold()
            if normalized in names:
                raise InvalidState("State paths collide on a destination platform")
            names.add(normalized)
            records.append({key: record[key] for key in ("root", "path", "size", "mtime_ns")})
            if len(records) > MAX_FILES:
                raise InvalidState("State inventory exceeds the current capture limit")
    validate_required_names(names)
    return sorted(records, key=lambda item: (item["root"], item["path"]))


def validate_required_names(names):
    if "config/local.json" not in names:
        raise InvalidState("Required application settings are missing")
    for pair in (("config/tls/ca.pem", "config/tls/ca-key.pem"),
                 ("config/tls/server.pem", "config/tls/server-key.pem")):
        if (pair[0] in names) != (pair[1] in names):
            raise InvalidState("Incomplete TLS certificate/key pair")


def source_path(roots, record):
    name = safe_member(f"{record['root']}/{record['path']}")
    path = roots[record["root"]].joinpath(*name.parts[1:])
    directory_identity(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise InvalidState("State source is not a regular file")
    if (info.st_size, info.st_mtime_ns) != (record["size"], record["mtime_ns"]):
        raise InvalidState("State changed during capture")
    return path


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        block = self.stream.read(size)
        self.digest.update(block)
        return block


def write_state_tar(roots, records, output):
    """Worker producer; validation failure is fatal even after tar bytes were sent."""
    if state_inventory(roots) != records:
        raise InvalidState("State inventory changed before capture")
    hashes = []
    with tarfile.open(fileobj=output, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for record in records:
            path = source_path(roots, record)
            with path.open("rb") as source:
                before = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (record["size"], record["mtime_ns"]):
                    raise InvalidState("State source changed before opening")
                reader = HashReader(source)
                member = tarfile.TarInfo(f"{record['root']}/{record['path']}")
                member.size = record["size"]
                member.mode = 0o600
                archive.addfile(member, reader)
                after = os.fstat(source.fileno())
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                    raise InvalidState("State source changed while reading")
                hashes.append(reader.digest.hexdigest())
    if state_inventory(roots) != records:
        raise InvalidState("State inventory changed during capture")
    for record, digest in zip(records, hashes, strict=True):
        with source_path(roots, record).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                raise InvalidState("State source content changed during capture")


def capture_state(config_root, data_root, directory, key, *, guard_factory=SpaceGuard, progress=None, resume=False):
    Fernet(key)
    roots = {"config": Path(os.path.abspath(config_root)), "data": Path(os.path.abspath(data_root))}
    directory = Path(os.path.abspath(directory))
    if directory.exists() and not resume:
        raise FileExistsError(directory)
    for root in roots.values():
        if directory.is_relative_to(root) or root.is_relative_to(directory):
            raise InvalidState("Backup output must be separate from state sources")
    if roots["config"].is_relative_to(roots["data"]) or roots["data"].is_relative_to(roots["config"]):
        raise InvalidState("Config and data roots must be separate")
    records = state_inventory(roots)
    size = sum(item["size"] for item in records)
    # PAX/long names and tar padding need space as well as the payload.
    estimated = size + len(records) * 16384 + 10240
    guard_factory([Requirement("destination", directory.parent, remaining_budget(directory, key, estimated))]).check()
    request = json.dumps({"roots": {kind: str(path) for kind, path in roots.items()}, "records": records}).encode()
    if len(request) > MAX_REQUEST:
        raise InvalidState("State capture request exceeds its limit")
    command = [sys.executable, "-m", "app.backup.state_files", "--worker"]
    from .diagnostics import ProcessDiagnostics
    diagnostics = ProcessDiagnostics()
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        reader = threading.Thread(target=diagnostics.drain, args=(process.stderr,), daemon=True)
        reader.start()
        try:
            process.stdin.write(request)
            process.stdin.close()

            def finish():
                result = process.wait(timeout=30)
                reader.join(timeout=5)
                if result or diagnostics.has_output or reader.is_alive():
                    raise diagnostics.failure(InvalidState, "File state changed or could not be captured; artifact not published")

            return seal_state_stream(process.stdout, directory, key, estimated_bytes=estimated, guard_factory=guard_factory,
                               finish=finish, progress=progress,
                               metadata={"kind": STATE_KIND, "file_count": len(records), "file_bytes": size,
                                         "atomic_library_snapshot": False, "requires_write_admission": True})
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(timeout=5)


class ChunkReader(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = memoryview(b"")

    def readable(self):
        return True

    def readinto(self, buffer):
        if not self.pending:
            self.pending = memoryview(next(self.chunks, b""))
        size = min(len(buffer), len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size


def walk_tar(directory, key, manifest, consume):
    metadata = manifest["metadata"]
    if (metadata.get("kind") != STATE_KIND or type(metadata.get("file_count")) is not int
            or not 1 <= metadata["file_count"] <= MAX_FILES or type(metadata.get("file_bytes")) is not int
            or not 0 <= metadata["file_bytes"] <= manifest["size"]):
        raise InvalidState("Invalid state artifact metadata")
    names = set()
    total = 0
    with io.BufferedReader(ChunkReader(read_chunks(directory, key, manifest))) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                name = safe_member(member.name)
                normalized = unicodedata.normalize("NFC", str(name)).casefold()
                if not member.isfile() or member.issparse() or normalized in names:
                    raise InvalidState("Duplicate, linked or special archive member")
                names.add(normalized)
                total += member.size
                if len(names) > metadata["file_count"] or member.size < 0 or total > metadata["file_bytes"]:
                    raise InvalidState("State archive exceeds declared inventory")
                with archive.extractfile(member) as source:
                    consume(name, member.size, source)
        # Exhaust authenticated chunks even if the tar reader stopped at its EOF.
        while stream.read(1024 * 1024):
            pass
    if len(names) != metadata["file_count"] or total != metadata["file_bytes"] or "config/local.json" not in names:
        raise InvalidState("Incomplete state archive inventory")
    validate_required_names(names)


def restore_state(directory, key, target, *, guard_factory=SpaceGuard):
    """Verify first, then write into a NEW private directory. Never start anything."""
    manifest = verify_artifact(directory, key)
    walk_tar(directory, key, manifest, lambda name, size, source: None)
    target = Path(os.path.abspath(target))
    artifact = Path(os.path.abspath(directory))
    if target.is_relative_to(artifact) or artifact.is_relative_to(target):
        raise InvalidState("Restore target must be separate from the artifact")
    guard_factory([Requirement("restore", target.parent, manifest["metadata"]["file_bytes"])]).check()
    create_private_directory(target)
    marker = target / "RESTORE-INCOMPLETE"
    marker.touch()
    guard = guard_factory([Requirement("restore", target, manifest["metadata"]["file_bytes"])])

    def write(name, size, source):
        output_path = target.joinpath(*name.parts)
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256()
        directory_identity(output_path.parent)
        with os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            remaining = size
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise InvalidState("Truncated state member")
                guard.check(next_role="restore", next_bytes=len(block))
                if output.write(block) != len(block):
                    raise OSError("Short state restore write")
                guard.written("restore", len(block))
                digest.update(block)
                remaining -= len(block)
            output.flush()
            os.fsync(output.fileno())
        with output_path.open("rb") as saved:
            if hashlib.file_digest(saved, "sha256").hexdigest() != digest.hexdigest():
                raise InvalidState("Restored state failed read-back verification")

    walk_tar(directory, key, manifest, write)
    report = {"kind": "restored-file-state-only", "files": manifest["metadata"]["file_count"],
              "activation_allowed": False, "database_restored": False}
    with (target / "RESTORE-STATE-ONLY.json").open("x", encoding="utf-8") as output:
        json.dump(report, output)
        output.flush()
        os.fsync(output.fileno())
    marker.unlink()
    sync_directory(target)
    return report


def worker():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise InvalidState("Oversized worker request")
        request = json.loads(raw)
        roots = {kind: Path(path) for kind, path in request["roots"].items()}
        write_state_tar(roots, request["records"], sys.stdout.buffer)
        return 0
    except Exception:
        print("State capture failed; no completion is permitted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(worker() if sys.argv[1:] == ["--worker"] else 2)
