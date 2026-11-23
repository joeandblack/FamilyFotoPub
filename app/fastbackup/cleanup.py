"""Explicit, preview-bound reclamation of an owned incomplete fast capture.

Keep the directory skeleton and ownership/password records for diagnosis. Never
infer ownership from a folder name, delete a completed capture, or follow links.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat

from cryptography.fernet import Fernet

from app.backup import library
from app.backup.encrypted import read_bounded
from app.backup.media_copy import copy_lock, sync_directory
from app.backup.space import directory_identity
from app.backup.sidecars import is_sidecar
from app.backup.volumes import same_except_device, volume_identity
from .native_copy import pin_directory


PAYLOADS = {'inventory', 'catalog', 'media-copy', 'library', library.THUMBNAILS}


def signature(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns]


def regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Cleanup refuses linked or non-regular files')
    return signature(info)


def owned_job(directory, key):
    identity = list(directory_identity(directory))
    regular(directory / library.JOB_RECORD)
    library.require_context(directory, key)
    sealed = json.loads(read_bounded(directory / library.JOB_RECORD, 65536))['sealed']
    job = json.loads(Fernet(key).decrypt(sealed.encode()))
    root = job['roots']['backup']
    if (job.get('format') != library.JOB_FORMAT or job.get('engine') != 'fastbackup-v1'
            or job.get('retire_to') is not None or root.get('path') != str(directory)
            or not same_except_device(root.get('identity'), identity)
            or root.get('volume') != volume_identity(directory)):
        raise ValueError('Cleanup requires an owned fast capture on its original volume and path')
    for role in ('config', 'data', 'media'):
        source = Path(job['roots'][role]['path'])
        if not source.is_absolute() or source.is_relative_to(directory) or directory.is_relative_to(source):
            raise ValueError('Cleanup target overlaps a source')
    if os.path.lexists(directory / 'library/complete.fernet'):
        raise ValueError('Completed captures cannot be cleaned up')
    journal = directory / 'journal.json'
    if journal.exists():
        regular(journal)
        record = json.loads(read_bounded(journal, 4096))
        if record.get('id') != job['id'] or record.get('phase') == 'complete':
            raise ValueError('Completed or mismatched capture journal')
    allowed = PAYLOADS | {library.JOB_RECORD, library.PASSWORD_FILE, library.PASSWORD_PART,
                          library.TIMING, library.HANDOVER, library.WINDOWS_HANDOVER,
                          'journal.json', 'journal.part', 'copy.lock'}
    for entry in directory.iterdir():
        if entry.name not in allowed and is_sidecar(entry, needs_sibling=False):
            regular(entry)
            continue
        if entry.name not in allowed and not library.abandoned_publication(entry, (library.JOB_RECORD, library.TIMING)):
            raise ValueError('Unrecognized capture entry; manual review required')
    return job


def snapshot(directory):
    """Validate the whole deletion set before removing the first file."""
    device = directory.stat().st_dev
    rows = []
    def visit(path):
        info = path.lstat()
        if (info.st_dev != device or getattr(info, 'st_file_attributes', 0) & 0x400
                or path.is_symlink() or os.path.ismount(path)):
            raise ValueError('Cleanup refuses links, reparse points and nested volumes')
        name = path.relative_to(directory).as_posix()
        if name == 'media-copy/copy.lock':
            regular(path)
            return  # Keep the worker's OS lock pathname while holding its lease.
        if stat.S_ISDIR(info.st_mode):
            directory_identity(path)
            rows.append([name, 'directory', [info.st_dev, info.st_ino]])
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            rows.append([name, 'file', regular(path)])
    for name in sorted(PAYLOADS):
        if os.path.lexists(directory / name):
            visit(directory / name)
    return rows


def describe_plan(directory, job, rows):
    raw = json.dumps([str(directory), job['id'], rows], sort_keys=True).encode()
    files = [row for row in rows if row[1] == 'file']
    return dict(capture_id=job['id'], expected_tree=hashlib.sha256(raw).hexdigest(),
                files=len(files), bytes=sum(row[2][4] for row in files),
                directory_retained=True, completed_backup=False)


def remove_file(directory, name, expected, directories):
    path = directory / name
    with ExitStack() as pins:
        for parent in (*reversed(path.parent.parents), path.parent):
            if parent != Path(parent.anchor):
                pin_directory(pins, parent)
            if parent.is_relative_to(directory) and parent != directory:
                if list(directory_identity(parent)) != directories[parent.relative_to(directory).as_posix()]:
                    raise ValueError('Cleanup directory changed')
        if regular(path) != expected:
            raise ValueError('Cleanup file changed after preview')
        if os.name == 'nt':
            from .windows_pinned_directory import DeletableFile
            from .native_copy import guid_path
            with DeletableFile(guid_path(path)) as handle:
                if regular(path) != expected:
                    raise ValueError('Cleanup file changed while opening')
                handle.remove()
        else:
            # Walk from a retained root fd: never traverse a newly inserted symlink.
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            pins.callback(os.close, fd)
            opened = os.fstat(fd)
            if [opened.st_dev, opened.st_ino] != directories['.']:
                raise ValueError('Cleanup root changed while opening')
            relative = Path(name)
            for index, part in enumerate(relative.parts[:-1], 1):
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                pins.callback(os.close, fd)
                info = os.fstat(fd)
                if [info.st_dev, info.st_ino] != directories[Path(*relative.parts[:index]).as_posix()]:
                    raise ValueError('Cleanup directory changed while opening')
            if signature(os.stat(relative.name, dir_fd=fd, follow_symlinks=False)) != expected:
                raise ValueError('Cleanup file changed while opening')
            os.unlink(relative.name, dir_fd=fd)
            os.fsync(fd)


def cleanup_incomplete(directory, key, *, expected_tree=None, confirm=False):
    directory = Path(os.path.abspath(directory))
    root_identity = directory_identity(directory)
    owned_job(directory, key)  # Authenticate before even creating the operation lock.
    with ExitStack() as pins:
        for parent in (*reversed(directory.parents), directory):
            if parent != Path(parent.anchor):
                pin_directory(pins, parent)
        with copy_lock(directory):
            job = owned_job(directory, key)
            if directory_identity(directory) != root_identity:
                raise ValueError('Cleanup root changed')
            media = directory / 'media-copy'
            if os.path.lexists(media):
                directory_identity(media)
                pins.enter_context(copy_lock(media))
            rows = snapshot(directory)
            plan = describe_plan(directory, job, rows)
            if confirm is not True:
                return plan
            if expected_tree != plan['expected_tree']:
                raise ValueError('Cleanup preview changed; inspect a new plan before confirming')
            directories = {name: identity for name, kind, identity in rows if kind == 'directory'}
            directories['.'] = list(root_identity)
            for name, kind, identity in rows:
                if kind == 'file':
                    remove_file(directory, name, identity, directories)
            sync_directory(directory)
            return {**plan, 'deleted_files': plan['files'], 'deleted_bytes': plan['bytes']}
