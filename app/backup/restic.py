"""Pinned local restic adapter. Consistency/admission belongs to the coordinator.

No network backend, password command, pruning, unlocking, or live activation is
offered here. A restic snapshot by itself is not a FamilyFoto recovery point.
"""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath, PurePosixPath
import queue
import re
import subprocess
import threading
import time

from .private_storage import create_private_directory
from .media_copy import sync_directory
from .space import directory_identity, Requirement, SpaceGuard
from .volumes import volume_identity


class ResticFailed(RuntimeError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('An exact full repository or snapshot ID is required')
    return value


def snapshot_root(path):
    """Convert stored source paths to restic tree paths, across host platforms."""
    if not isinstance(path, str) or len(path) > 32768:
        raise ValueError('Invalid snapshot source root')
    if re.match(r'^[a-zA-Z]:[\\/]', path):
        windows = PureWindowsPath(path)
        value = '/' + windows.drive[0].upper() + '/' + '/'.join(windows.parts[1:])
    else:
        value = path
    if (not value.startswith('/') or '\\' in value or ':' in value
            or any(part in {'', '.', '..'} for part in value.split('/')[1:])):
        raise ValueError('Unsupported snapshot source root')
    return str(PurePosixPath(value))


class Restic:
    def __init__(self, executable, executable_sha256, repository, password_file):
        self.executable = Path(executable).absolute()
        self.executable_sha256 = identifier(executable_sha256)
        self.repository = Path(repository).absolute()
        self.password_file = Path(password_file).absolute()
        directory_identity(self.repository.parent)
        directory_identity(self.password_file.parent)
        if (self.password_file.is_symlink() or not self.password_file.is_file()
                or not 1 <= self.password_file.stat().st_size <= 4096
                or self.password_file.is_relative_to(self.repository)):
            raise ValueError('A separate regular repository password file is required')
        self.verify_executable()

    def verify_executable(self):
        directory_identity(self.executable.parent)
        if self.executable.is_symlink() or not self.executable.is_file():
            raise ValueError('An explicit regular restic executable is required')
        with self.executable.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != self.executable_sha256:
                raise ValueError('Restic executable checksum mismatch')

    def environment(self):
        # Ignore inherited restic options, especially password-command and backend
        # overrides. Only local absolute repository paths are accepted by this API.
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('RESTIC_')}
        env['RESTIC_PASSWORD_FILE'] = str(self.password_file)
        env['RESTIC_PROGRESS_FPS'] = '1'
        return env

    def run(self, arguments, *, handler=None, guard=None, timeout=None, cwd=None):
        self.verify_executable()
        command = [str(self.executable), '--repo', str(self.repository), '--no-cache', *arguments]
        chunks = queue.Queue(maxsize=32)
        finished = object()
        output = bytearray()
        started = time.monotonic()
        with subprocess.Popen(command, cwd=cwd, env=self.environment(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
            stopping = threading.Event()
            def send(value):
                while not stopping.is_set():
                    try:
                        chunks.put(value, timeout=.2)
                        return
                    except queue.Full:
                        pass

            def read_output():
                try:
                    while block := process.stdout.readline(65537):
                        send(block)
                finally:
                    send(finished)

            def discard_errors():
                # Backend diagnostics may contain private filenames; don't persist
                # them or pass them to the browser. Exit codes remain authoritative.
                while process.stderr.read(65536):
                    pass

            readers = [threading.Thread(target=read_output, daemon=True),
                       threading.Thread(target=discard_errors, daemon=True)]
            for reader in readers:
                reader.start()
            try:
                while True:
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise ResticFailed('Backup engine response timed out; incomplete work retained')
                    if guard:
                        guard.check()
                    try:
                        block = chunks.get(timeout=.2)
                    except queue.Empty:
                        continue
                    if block is finished:
                        break
                    if len(block) > 65536:
                        raise ResticFailed('Backup engine output exceeded a bounded record')
                    if handler:
                        handler(block)
                    else:
                        output.extend(block)
                        if len(output) > 16 * 1024 ** 2:
                            raise ResticFailed('Backup engine response exceeded its limit')
                code = process.wait(timeout=30)
                if code != 0:
                    raise ResticFailed(f'Backup engine failed (exit {code}); snapshot is not approved for recovery')
                if guard:
                    guard.check()
                return bytes(output)
            finally:
                stopping.set()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                for reader in readers:
                    reader.join(timeout=5)

    def initialize(self, *, allow_unprotected=False):
        create_private_directory(self.repository, allow_unprotected=allow_unprotected)
        self.run(['init', '--repository-version', '2', '--json'], timeout=120)
        return self.repository_id()

    def repository_id(self):
        directory_identity(self.repository)
        data = json.loads(self.run(['cat', 'config'], timeout=120))
        if data.get('version') != 2:
            raise ResticFailed('Unsupported encrypted repository format')
        return identifier(data.get('id'))

    def check(self, expected_repository):
        if self.repository_id() != identifier(expected_repository):
            raise ResticFailed('Repository identity changed')
        self.run(['check', '--read-data'], handler=lambda block: None)

    def snapshot(self, snapshot_id):
        value = json.loads(self.run(['cat', 'snapshot', identifier(snapshot_id)], timeout=120))
        paths = value.get('paths') if isinstance(value, dict) else None
        if not isinstance(paths, list) or len(paths) != 1:
            raise ResticFailed('Expected a single-root FamilyFoto snapshot')
        value['restore_root'] = snapshot_root(paths[0])
        return value

    def backup(self, source, expected_repository, capture_id, *, estimated_bytes,
               guard_factory=SpaceGuard, progress=None):
        source = Path(source).absolute()
        directory_identity(source)
        if (source.is_relative_to(self.repository) or self.repository.is_relative_to(source)
                or self.password_file.is_relative_to(source)):
            raise ValueError('Source, repository and password must be separate')
        if not re.fullmatch('[0-9a-f]{32}', capture_id):
            raise ValueError('Explicit recovery capture ID required')
        if self.repository_id() != identifier(expected_repository):
            raise ResticFailed('Repository identity changed')
        pinned_volume = volume_identity(self.repository)
        guard = guard_factory([Requirement('repository', self.repository, estimated_bytes)])
        guard.check()
        # Preflight budgets worst-case growth. During the engine run the reserve
        # remains enforced, but processed bytes cannot stand in for allocated bytes
        # because deduplication changes allocation. Backend ENOSPC is still fatal.
        guard.written('repository', estimated_bytes)
        summaries = []
        def event(block):
            try:
                value = json.loads(block)
            except (ValueError, UnicodeError) as error:
                raise ResticFailed('Invalid backup progress response') from error
            if not isinstance(value, dict):
                raise ResticFailed('Invalid backup progress response')
            if value.get('message_type') == 'error':
                raise ResticFailed('A required source could not be backed up')
            if value.get('message_type') == 'summary':
                summaries.append(value)
                if len(summaries) > 1:
                    raise ResticFailed('Ambiguous backup completion response')
            elif value.get('message_type') == 'status' and progress:
                progress({key: value[key] for key in ('percent_done', 'total_files', 'files_done',
                                                     'total_bytes', 'bytes_done') if key in value})
        self.run(['backup', '--json', '--force', '--host', 'familyfoto', '--tag', 'familyfoto:' + capture_id,
                  '--', str(source)], handler=event, guard=guard)
        if len(summaries) != 1:
            raise ResticFailed('Backup engine did not confirm a snapshot')
        snapshot = identifier(summaries[0].get('snapshot_id'))
        self.check(expected_repository)
        if volume_identity(self.repository) != pinned_volume:
            raise ResticFailed('Repository disk changed during backup')
        return {'repository_id': expected_repository, 'snapshot_id': snapshot,
                'capture_id': capture_id, 'repository_read_verified': True,
                'data_added': summaries[0].get('data_added'),
                'familyfoto_recovery_point': False}

    def restore(self, snapshot, expected_repository, target, *, required_bytes, guard_factory=SpaceGuard,
                root_only=False):
        snapshot = identifier(snapshot)
        if self.repository_id() != identifier(expected_repository):
            raise ResticFailed('Repository identity changed')
        selection = snapshot + ':' + self.snapshot(snapshot)['restore_root'] if root_only else snapshot
        target = Path(target).absolute()
        if target.is_relative_to(self.repository) or self.repository.is_relative_to(target):
            raise ValueError('Restore target must be independent of the repository')
        create_private_directory(target)
        marker = target / 'RESTIC-RESTORE-INCOMPLETE'
        with marker.open('xb') as handle:
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(target)
        guard = guard_factory([Requirement('restore', target, required_bytes)])
        guard.check()
        guard.written('restore', required_bytes)
        self.run(['restore', selection, '--target', str(target), '--verify', '--overwrite', 'never'],
                 handler=lambda block: None, guard=guard)
        marker.unlink()
        sync_directory(target)
        return {'snapshot_id': snapshot, 'restored': True, 'activation_allowed': False}
