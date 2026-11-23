"""Explicitly resumable, isolated encrypted snapshot restore jobs."""
import json
import os
from pathlib import Path
import stat

from .media_copy import copy_lock, sync_directory
from .private_storage import create_private_directory
from .restic import identifier, ResticFailed
from .space import directory_identity, Requirement, SpaceGuard
from .volumes import same_job, volume_identity


def regular_tree(root):
    """Never let a retry traverse a linked or externally mounted output tree."""
    device = root.stat().st_dev
    def unreadable(error):
        raise error

    for base, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in directories + files:
            path = Path(base) / name
            info = path.lstat()
            if (path.is_symlink() or path.is_junction() or info.st_dev != device
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise ResticFailed('Restore job contains linked or unsupported output')


def restore_snapshot_job(engine, snapshot, repository, directory, *, required_bytes,
                         resume=False, reuse_verified=False, guard_factory=SpaceGuard):
    """Return payload only after engine read-back; never create/activate a database.

    Controls are outside the snapshot payload. Explicit retry may repair that
    payload only, never another target or a completed recovery installation.
    """
    snapshot, repository = identifier(snapshot), identifier(repository)
    directory = Path(os.path.abspath(directory))
    if (directory.is_relative_to(engine.repository) or engine.repository.is_relative_to(directory)
            or engine.password_file.is_relative_to(directory)):
        raise ValueError('Restore job, repository and password must be independent')
    if resume:
        directory_identity(directory)
    else:
        create_private_directory(directory)
    with copy_lock(directory):
        allowed = {'copy.lock', 'job.json', 'payload', 'INCOMPLETE', 'VERIFIED'}
        if any(path.name not in allowed or path.is_symlink() or path.is_junction()
               for path in directory.iterdir()):
            raise ResticFailed('Unexpected restore job entry')
        for name in ('job.json', 'INCOMPLETE', 'VERIFIED'):
            path = directory / name
            if path.exists():
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or (name != 'job.json' and info.st_size != 0)):
                    raise ResticFailed('Invalid restore job control file')
        selection = snapshot + ':' + engine.snapshot(snapshot)['restore_root']
        expected = {'format': 'familyfoto-restic-restore-job-v1', 'repository': repository,
                    'snapshot': snapshot, 'selection': selection,
                    'directory': list(directory_identity(directory)),
                    'volume': volume_identity(directory)}
        record = directory / 'job.json'
        payload = directory / 'payload'
        if resume:
            if not record.is_file() or record.stat().st_size > 65536:
                raise ResticFailed('Missing or oversized restore job identity')
            if not same_job(json.loads(record.read_text(encoding='utf-8')), expected, 'directory'):
                raise ResticFailed('Restore job identity changed')
            if reuse_verified and (directory / 'VERIFIED').is_file() and not (directory / 'INCOMPLETE').exists():
                directory_identity(payload)
                regular_tree(payload)
                return payload
            if not (directory / 'INCOMPLETE').is_file():
                raise ResticFailed('Only an unfinished restore job can be resumed')
            directory_identity(payload)
            regular_tree(payload)
        else:
            create_private_directory(payload)
            with record.open('x', encoding='utf-8') as stream:
                json.dump(expected, stream)
                stream.flush()
                os.fsync(stream.fileno())
            with (directory / 'INCOMPLETE').open('xb') as stream:
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(directory)
        engine.check(repository)
        guard = guard_factory([Requirement('restore', directory, required_bytes)])
        guard.check()
        guard.written('restore', required_bytes)
        engine.run(['restore', selection, '--target', str(payload), '--verify', '--overwrite', 'always'],
                   handler=lambda block: None, guard=guard)
        regular_tree(payload)
        if (volume_identity(directory) != expected['volume']
                or list(directory_identity(directory)) != expected['directory']):
            raise ResticFailed('Restore destination changed')
        # Higher-level inventory verification still decides application validity.
        # Both markers can survive a shutdown. That is still unfinished: retry
        # the full engine verification above, never trust VERIFIED alone then.
        if not (directory / 'VERIFIED').exists():
            with (directory / 'VERIFIED').open('xb') as stream:
                stream.flush()
                os.fsync(stream.fileno())
        sync_directory(directory)
        (directory / 'INCOMPLETE').unlink()
        sync_directory(directory)
        return payload
