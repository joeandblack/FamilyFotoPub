"""Cross-process offline admission primitive with fail-closed crash recovery.

All source writers must hold a participant lease for their complete lifetime.
This primitive is not sufficient until each write-capable entry point is enrolled.
"""
from contextlib import contextmanager
import atexit
import json
import os
from pathlib import Path
import uuid
import threading

from .media_copy import copy_lock, sync_directory
from .private_storage import create_private_directory
from .space import directory_identity


class MaintenanceBlocked(RuntimeError):
    pass


_process_leases = {}
_lease_lock = threading.RLock()


def guard_application_path(path):
    """Enroll known application state writers that do not open a DB connection."""
    from ..config import DATA, configuration_root
    path = Path(path).absolute()
    if path.is_relative_to(DATA.absolute()) or path.is_relative_to(configuration_root()):
        hold_process_lease(DATA)


def hold_process_lease(root):
    """Enroll a process before its first application write; release only at exit."""
    root = Path(root).absolute()
    with _lease_lock:
        key = (os.getpid(), root)
        if key in _process_leases:
            identity, _ = _process_leases[key]
            if directory_identity(root) != identity:
                raise MaintenanceBlocked('Application state directory changed')
            return
        # A fresh installation may create its state root. Existing maintenance
        # roots are never removed or replaced by this initializer.
        root.mkdir(parents=True, exist_ok=True)
        identity = directory_identity(root)
        lease = participant(root)
        lease.__enter__()
        _process_leases[key] = (identity, lease)
        atexit.register(lease.__exit__, None, None, None)


def prepare(root):
    root = Path(root).absolute()
    directory_identity(root)
    directory = root / '.recovery-admission'
    if not directory.exists():
        try:
            make_lease_directory(directory)
        except FileExistsError:
            pass  # Another participant created the gate first.
    directory_identity(directory)
    return directory


def make_lease_directory(path):
    # An elevated Windows creator can have Administrators as its default owner.
    # Grant the account SID explicitly so its limited desktop token can use the lease.
    if os.name == 'nt':
        create_private_directory(path)
    else:
        path.mkdir(mode=0o700)


@contextmanager
def participant(root):
    """Register a writer. A crashed process releases its OS lease automatically."""
    directory = prepare(root)
    lease = None
    owner = None
    try:
        with copy_lock(directory):
            if (directory / 'retired.json').exists() or (directory / 'retired.json').is_symlink():
                raise MaintenanceBlocked('This library installation is retired; use its activated replacement')
            if (directory / 'maintenance.json').exists():
                raise MaintenanceBlocked('Recovery maintenance is active or needs recovery')
            owner = directory / uuid.uuid4().hex
            make_lease_directory(owner)
            acquired = copy_lock(owner)
            acquired.__enter__()
            lease = acquired
        yield
    finally:
        if lease is not None:
            lease.__exit__(None, None, None)
        # Leave an empty/stale lease for maintenance to collect while holding the
        # registration latch. Never remove a lock pathname another process holds.


def collect_idle(directory):
    for owner in directory.iterdir():
        if owner.name in {'copy.lock', 'maintenance.json', 'library.json', 'retired.json'}:
            if owner.is_symlink() or owner.is_junction() or not owner.is_file():
                raise MaintenanceBlocked('Unsafe recovery admission state')
            continue
        if len(owner.name) != 32 or any(char not in '0123456789abcdef' for char in owner.name):
            raise MaintenanceBlocked('Unexpected recovery admission state')
        directory_identity(owner)
        if any(child.name != 'copy.lock' for child in owner.iterdir()):
            raise MaintenanceBlocked('Unexpected participant lease state')
        try:
            with copy_lock(owner):
                pass
        except OSError as exc:
            raise MaintenanceBlocked('Stop application and worker processes before backup') from exc
        (owner / 'copy.lock').unlink(missing_ok=True)
        owner.rmdir()


@contextmanager
def maintenance(root, operation):
    """Hold the registration latch throughout capture; failed capture stays gated."""
    if operation not in {'backup', 'restore', 'relocate', 'upgrade'}:
        raise ValueError('Unknown maintenance operation')
    directory = prepare(root)
    with copy_lock(directory):
        if (directory / 'retired.json').exists() or (directory / 'retired.json').is_symlink():
            raise MaintenanceBlocked('This library installation is retired; maintenance is not admitted')
        marker = directory / 'maintenance.json'
        if marker.exists():
            raise MaintenanceBlocked('Previous maintenance requires explicit recovery')
        collect_idle(directory)
        identity = uuid.uuid4().hex
        # The marker is never interpreted as proof that a backup succeeded.
        with marker.open('x', encoding='utf-8') as output:
            json.dump({'id': identity, 'operation': operation, 'state': 'in-progress'}, output)
            output.flush()
            os.fsync(output.fileno())
        sync_directory(directory)
        yield identity
        marker.unlink()
        sync_directory(directory)


def acknowledge_failed_maintenance(root, expected_id):
    """Explicit operator acknowledgement only; never resumes work or removes backups."""
    directory = prepare(root)
    with copy_lock(directory):
        collect_idle(directory)
        marker = directory / 'maintenance.json'
        if marker.is_symlink() or marker.stat().st_size > 4096:
            raise MaintenanceBlocked('Invalid maintenance marker')
        record = json.loads(marker.read_text(encoding='utf-8'))
        if record.get('id') != expected_id:
            raise MaintenanceBlocked('Maintenance identity changed')
        marker.unlink()
        sync_directory(directory)


@contextmanager
def resume_maintenance(root, expected_id, operation):
    """Resume an owned interrupted operation, never an already released gate."""
    from .encrypted import read_bounded

    directory = prepare(root)
    with copy_lock(directory):
        collect_idle(directory)
        if (directory / 'retired.json').exists():
            raise MaintenanceBlocked('This library installation is retired')
        marker = directory / 'maintenance.json'
        if not marker.exists():
            raise MaintenanceBlocked('Maintenance was already released; rollback is no longer safe')
        record = json.loads(read_bounded(marker, 4096))
        if record != {'id': expected_id, 'operation': operation, 'state': 'in-progress'}:
            raise MaintenanceBlocked('Maintenance ownership changed')
        yield
        marker.unlink()
        sync_directory(directory)
