"""Measure what a backup destination's filesystem really does, using synthetic files only.

Usage, from the repository root:

    python scripts/probe_backup_filesystem.py --root DESTINATION_FOLDER

To test a real disconnect, run this, then eject, unplug and reconnect the disk:

    python scripts/probe_backup_filesystem.py --root DESTINATION_FOLDER --prepare-remount
    python scripts/probe_backup_filesystem.py --check-remount THE_PREPARED_FOLDER

Everything happens inside one new folder under --root, plus one system temporary
folder. Both are removed afterwards unless --keep is given. Nothing else on the disk is
read, changed or deleted, and no library, database or service is touched. The JSON
report uses only synthetic names. A failed step is a measurement, not an error.
"""
import argparse
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import traceback
import unicodedata
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backup.media_copy import copy_lock, copy_media_file, sync_directory  # noqa: E402
from app.backup.media_inventory import capture_media_inventory, verify_media_inventory  # noqa: E402
from app.backup.media_tree import copy_media_tree, verify_media_tree  # noqa: E402
from app.backup.private_storage import create_private_directory, UnprotectedDestination  # noqa: E402
from app.backup.recovery import save_private  # noqa: E402
from app.backup.space import directory_identity  # noqa: E402
from app.backup.volumes import volume_identity  # noqa: E402


FILES = {'album/one.jpg': b'one' * 4096, 'album/two.jpg': b'two' * 4096, 'three.jpg': b'three',
         'empty.bin': b'', '照片.jpg': b'cjk'}


def attempt(function):
    """Return the function's value, or how it failed, as plain data."""
    try:
        return {'ok': True, 'value': function()}
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        return {'ok': False, 'error': type(error).__name__, 'message': str(error)[:200],
                'errno': getattr(error, 'errno', None), 'at': f'{Path(frame.filename).name}:{frame.lineno}'}


def filesystem_name(root):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        name = ctypes.create_unicode_buffer(64)
        anchor = Path(root).anchor
        if not kernel.GetVolumeInformationW(anchor, None, 0, None, None, None, name, len(name)):
            raise OSError('Cannot read volume information')
        return name.value
    if sys.platform == 'darwin':
        mount = Path(root)
        while not os.path.ismount(mount):
            mount = mount.parent
        output = subprocess.run(['/usr/sbin/diskutil', 'info', '-plist', str(mount)],
                                capture_output=True, timeout=15, check=True).stdout
        return plistlib.loads(output).get('FilesystemType')
    return 'unknown'


def hard_link(directory):
    (directory / 'link-source').write_bytes(b'x')
    os.link(directory / 'link-source', directory / 'link-target')
    return os.stat(directory / 'link-source').st_nlink


def rename_refuses_existing(directory):
    (directory / 'rename-source').write_bytes(b'new')
    (directory / 'rename-target').write_bytes(b'old')
    try:
        os.rename(directory / 'rename-source', directory / 'rename-target')
    except OSError as error:
        return {'refused': True, 'errno': error.errno,
                'target_kept': (directory / 'rename-target').read_bytes() == b'old'}
    return {'refused': False, 'target_kept': (directory / 'rename-target').read_bytes() == b'old'}


def rename_exclusive(directory):
    """macOS atomic no-clobber rename, the likely replacement for hard links."""
    if sys.platform != 'darwin':
        return 'not-applicable'
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.renamex_np
    function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    exclusive = 0x00000004
    source, free, taken = (directory / name for name in ('excl-source', 'excl-free', 'excl-taken'))
    source.write_bytes(b'new')
    taken.write_bytes(b'old')
    refused = function(os.fsencode(source), os.fsencode(taken), exclusive)
    refused_errno = ctypes.get_errno()
    moved = function(os.fsencode(source), os.fsencode(free), exclusive)
    moved_errno = ctypes.get_errno()
    return {'onto_existing_result': refused, 'onto_existing_errno': refused_errno,
            'target_kept': taken.read_bytes() == b'old', 'onto_free_result': moved,
            'onto_free_errno': moved_errno if moved else 0, 'moved': free.exists()}


def exclusive_create(directory):
    path = directory / 'exclusive'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return 'second-create-refused'
    return 'second-create-allowed'


def permissions(directory):
    path = directory / 'mode'
    path.mkdir()
    path.chmod(0o700)
    child = path / 'file'
    child.write_bytes(b'x')
    child.chmod(0o600)
    return {'directory_mode': oct(stat.S_IMODE(path.stat().st_mode)), 'file_mode': oct(stat.S_IMODE(child.stat().st_mode))}


def identities(directory):
    """Whether inode numbers hold still through what a job does, within this process."""
    folder = directory / 'stable'
    folder.mkdir()
    file = folder / 'file'
    file.write_bytes(b'x')
    first = (os.stat(folder).st_dev, os.stat(folder).st_ino, os.stat(file).st_ino)
    (folder / 'other').write_bytes(b'y')
    with file.open('ab') as stream:
        stream.write(b'more')
        stream.flush()
        os.fsync(stream.fileno())
        opened = os.fstat(stream.fileno()).st_ino
    renamed = folder / 'renamed'
    file.rename(renamed)
    return {'device_nonzero': first[0] != 0, 'directory_inode_nonzero': first[1] != 0,
            'file_inode_nonzero': first[2] != 0,
            'directory_inode_stable': os.stat(folder).st_ino == first[1],
            'open_matches_path_inode': opened == first[2],
            'file_inode_stable_after_rename': os.stat(renamed).st_ino == first[2],
            'link_count': os.stat(renamed).st_nlink}


def timestamps(directory):
    path = directory / 'time'
    path.write_bytes(b'x')
    wanted = 1_700_000_000_123_456_789
    os.utime(path, ns=(wanted, wanted))
    seen = os.stat(path).st_mtime_ns
    written = directory / 'time-written'
    with written.open('wb') as stream:
        stream.write(b'x')
        stream.flush()
        os.fsync(stream.fileno())
        while_open = os.fstat(stream.fileno()).st_mtime_ns
    return {'requested_ns': wanted, 'stored_ns': seen, 'lost_ns': wanted - seen,
            'written_mtime_changes_after_close': os.stat(written).st_mtime_ns != while_open,
            'written_mtime_ns_remainder_2s': os.stat(written).st_mtime_ns % 2_000_000_000}


def private_folder_access(directory):
    """What actually protects a folder made by create_private_directory."""
    report = {}
    try:
        create_private_directory(directory / 'private-default')
        report['default'] = 'created'
    except UnprotectedDestination:
        report['default'] = 'refused'
        report['refused_folder_removed'] = not (directory / 'private-default').exists()
    path = create_private_directory(directory / 'private-check', allow_unprotected=True)
    (path / 'file').write_bytes(b'x')
    report['directory_mode'] = oct(stat.S_IMODE(path.stat().st_mode))
    if os.name == 'nt':
        system = Path(os.environ['SystemRoot']) / 'System32'
        options = {'capture_output': True, 'creationflags': subprocess.CREATE_NO_WINDOW, 'timeout': 15}
        result = subprocess.run([str(system / 'icacls.exe'), str(path)], **options)
        report['icacls_exit'] = result.returncode
        report['acl_entries_listed'] = sum(b':' in line and b'(' in line for line in result.stdout.splitlines())
        report['everyone_listed'] = b'Everyone' in result.stdout
    return report


def names(directory):
    folder = directory / 'names'
    folder.mkdir()
    composed = unicodedata.normalize('NFC', 'café')
    decomposed = unicodedata.normalize('NFD', composed)
    (folder / composed).write_bytes(b'x')
    report = {'keeps_composed': os.listdir(folder) == [composed]}
    report['decomposed_is_same_file'] = (folder / decomposed).exists()
    (folder / 'Case').write_bytes(b'x')
    report['case_insensitive'] = (folder / 'case').exists()

    def longest(character):
        best = 0
        for count in range(1, 257):
            path = folder / (character * count)
            try:
                with open(os.path.abspath(path) if os.name != 'nt' else '\\\\?\\' + os.path.abspath(path), 'wb'):
                    pass
            except OSError:
                break
            os.remove(os.path.abspath(path) if os.name != 'nt' else '\\\\?\\' + os.path.abspath(path))
            best = count
        return best
    report.update(longest_ascii=longest('a'), longest_cjk=longest('照'), longest_emoji=longest('\U0001F600'))
    return report


def symlink(directory):
    os.symlink(directory / 'time', directory / 'symbolic-link')
    return 'created'


def sidecars(directory):
    """Anything the operating system adds beside ordinary writes shows up here."""
    folder = directory / 'sidecars'
    folder.mkdir()
    (folder / 'file.jpg').write_bytes(b'x')
    os.utime(folder / 'file.jpg')
    (folder / 'file.jpg').chmod(0o600)
    return sorted(name for name in os.listdir(folder) if name != 'file.jpg')


def directory_sync(directory):
    sync_directory(directory)
    return 'synced' if os.name != 'nt' else 'not-applicable'


def locks(directory):
    folder = directory / 'locks'
    folder.mkdir()
    with copy_lock(folder):
        try:
            with copy_lock(folder):
                return 'second-lock-allowed'
        except OSError:
            return 'second-lock-refused'


def sqlite_database(directory):
    path = directory / 'test.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE fixture(id INTEGER)')
        db.execute('INSERT INTO fixture VALUES (1)')
        db.commit()
        return db.execute('SELECT count(*) FROM fixture').fetchone()[0]


def source_files(root):
    for name, raw in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


class Stop(Exception):
    pass


def local_folder(prefix):
    # macOS keeps temporary folders under a symlink, which the backup code refuses.
    return Path(tempfile.mkdtemp(prefix=prefix)).resolve()


def remove_folder(path):
    """Remove one of this script's folders, and report anything it could not."""
    failures = []

    def retry(function, name, error):
        # Removing a file on macOS exFAT also removes its sidecar, so that one is already gone.
        if isinstance(error, FileNotFoundError) and Path(name).name.startswith('._'):
            return
        # macOS exFAT can list a name in a spelling only another form of it can remove.
        for form in ('NFC', 'NFD'):
            spelled = unicodedata.normalize(form, os.fspath(name))
            if spelled != os.fspath(name):
                try:
                    function(spelled)
                    return
                except OSError:
                    pass
        failures.append(type(error).__name__)
    shutil.rmtree(path, onexc=retry)
    return failures


def stopped(function):
    """Run until the Stop this probe raises. Report any other failure, which is a measurement."""
    try:
        function()
    except Stop:
        return None
    except Exception as error:
        return f'{type(error).__name__}: {str(error)[:120]}'
    return None


def real_code(directory, local):
    """The actual backup primitives, with this disk as destination and as source."""
    results = {}
    results['private_directory'] = attempt(lambda: create_private_directory(
        directory / 'private', allow_unprotected=True).name)
    results['save_private'] = attempt(lambda: save_private(directory / 'saved.json', {'fixture': True}))
    source = local / 'media'
    source.mkdir()
    source_files(source)
    inventory = local / 'inventory'
    results['inventory_local'] = attempt(lambda: capture_media_inventory(
        source, inventory, offline_confirmed=True)['files'])

    single_job = directory / 'single-file-job'
    single_job.mkdir()
    digest = hashlib.sha256(FILES['three.jpg']).hexdigest()
    results['copy_one_file'] = attempt(lambda: copy_media_file(source / 'three.jpg', single_job, digest).name)

    target = directory / 'copy-tree'
    results['copy_tree'] = attempt(lambda: copy_media_tree(
        source, inventory, target, offline_confirmed=True, allow_unprotected=True)['files'])
    results['verify_tree'] = attempt(lambda: verify_media_tree(target)['files'])
    results['entries_after_copy'] = attempt(lambda: sorted(entry.name for entry in target.iterdir()))
    results['repeat_copy'] = attempt(lambda: copy_media_tree(
        source, inventory, target, offline_confirmed=True, allow_unprotected=True)['files'])

    resumed = directory / 'copy-resumed'

    def stop_after_first(event):
        if event.get('phase') == 'copy':
            raise Stop

    def interrupted():
        try:
            copy_media_tree(source, inventory, resumed, offline_confirmed=True, progress=stop_after_first,
                            allow_unprotected=True)
        except Stop:
            return 'interrupted'
    results['interrupt_copy'] = attempt(interrupted)
    results['resume_copy'] = attempt(lambda: copy_media_tree(
        source, inventory, resumed, offline_confirmed=True, allow_unprotected=True)['files'])
    results['verify_resumed'] = attempt(lambda: verify_media_tree(resumed)['files'])

    # This disk as the source, as when a backup is restored from it.
    disk_source = directory / 'disk-media'
    disk_source.mkdir()
    source_files(disk_source)
    disk_inventory = directory / 'disk-inventory'
    results['inventory_on_disk'] = attempt(lambda: capture_media_inventory(
        disk_source, disk_inventory, offline_confirmed=True, allow_unprotected=True)['files'])
    results['verify_inventory_on_disk'] = attempt(lambda: verify_media_inventory(disk_inventory, disk_source)['files'])
    results['copy_from_disk'] = attempt(lambda: copy_media_tree(
        disk_source, disk_inventory, local / 'restored', offline_confirmed=True)['files'])
    results['verify_copy_from_disk'] = attempt(lambda: verify_media_tree(local / 'restored')['files'])
    return results


def stats(root):
    """Device, inode, size and time of every entry, which a remount must not change."""
    report = {}
    for base, folders, files in os.walk(root):
        for name in [*folders, *files]:
            path = Path(base) / name
            info = path.stat(follow_symlinks=False)
            report[path.relative_to(root).as_posix()] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    return report


def prepare_remount(root):
    """First half of a real disconnect test: leave interrupted work for --check-remount."""
    root = Path(os.path.abspath(root))
    directory = root / f'familyfoto-remount-{uuid.uuid4().hex}'
    directory.mkdir()
    local = local_folder('familyfoto-remount-local-')
    source = local / 'media'
    source.mkdir()
    source_files(source)
    capture_media_inventory(source, local / 'inventory', offline_confirmed=True)
    disk_source = directory / 'disk-media'
    disk_source.mkdir()
    source_files(disk_source)
    capture_media_inventory(disk_source, directory / 'disk-inventory', offline_confirmed=True, allow_unprotected=True)

    def stop_copy(event):
        if event.get('phase') == 'copy':
            raise Stop
    tree_failure = stopped(lambda: copy_media_tree(source, local / 'inventory', directory / 'copy-tree',
                                                   offline_confirmed=True, progress=stop_copy, allow_unprotected=True))
    partial = directory / 'partial-file'
    partial.mkdir()
    raw = bytes(range(256)) * 4

    def stop_file(offset, size):
        raise Stop
    # Outside the inventoried media, which must not change before the tree copy resumes.
    single = local / 'single'
    single.mkdir()
    (single / 'partial.bin').write_bytes(raw)
    file_failure = stopped(lambda: copy_media_file(single / 'partial.bin', partial, hashlib.sha256(raw).hexdigest(),
                                                   chunk_size=64, progress=stop_file))
    state = {'local': str(local), 'volume': volume_identity(directory), 'directory': list(directory_identity(directory)),
             'media': list(directory_identity(disk_source)), 'stats': stats(directory),
             'partial_sha256': hashlib.sha256(raw).hexdigest(), 'tree_failure': tree_failure, 'file_failure': file_failure}
    (directory / 'remount-state.json').write_text(json.dumps(state))
    return {'prepared': str(directory), 'entries': len(state['stats']),
            'tree_failure': tree_failure, 'file_failure': file_failure}


def check_remount(directory):
    """Second half, after the disk was really ejected, unplugged and plugged back in."""
    directory = Path(os.path.abspath(directory))
    if not directory.name.startswith('familyfoto-remount-'):
        raise ValueError('Not a prepared remount folder')
    state = json.loads((directory / 'remount-state.json').read_text())
    local, before = Path(state['local']), state['stats']
    after = stats(directory)
    after.pop('remount-state.json', None)
    fields = ('device', 'inode', 'size', 'mtime')
    changed = {field: sum(before[name][index] != after[name][index] for name in before if name in after)
               for index, field in enumerate(fields)}
    report = {'filesystem': attempt(lambda: filesystem_name(directory)),
              'volume_same': volume_identity(directory) == state['volume'],
              'directory_device_same': directory_identity(directory)[0] == state['directory'][0],
              'directory_inode_same': directory_identity(directory)[1] == state['directory'][1],
              'media_inode_same': directory_identity(directory / 'disk-media')[1] == state['media'][1],
              'entries_before': len(before), 'entries_missing': sum(name not in after for name in before),
              'entries_changed': changed,
              'mtime_lost_ns_examples': sorted({before[name][3] - after[name][3] for name in before
                                                if name in after and before[name][3] != after[name][3]})[:6]}
    report['verify_inventory_on_disk'] = attempt(lambda: verify_media_inventory(
        directory / 'disk-inventory', directory / 'disk-media')['files'])
    report['prepare_failures'] = {'tree': state.get('tree_failure'), 'file': state.get('file_failure')}
    report['resume_partial_file'] = attempt(lambda: copy_media_file(
        local / 'single/partial.bin', directory / 'partial-file', state['partial_sha256']).name)
    report['resume_copy_tree'] = attempt(lambda: copy_media_tree(
        local / 'media', local / 'inventory', directory / 'copy-tree', offline_confirmed=True,
        allow_unprotected=True)['files'])
    report['verify_tree'] = attempt(lambda: verify_media_tree(directory / 'copy-tree')['files'])
    shutil.rmtree(local, ignore_errors=True)
    report['cleanup_failures'] = remove_folder(directory)
    report['removed'] = not directory.exists()
    return report


def probe(root, keep):
    root = Path(os.path.abspath(root))
    directory = root / f'familyfoto-probe-{uuid.uuid4().hex}'
    directory.mkdir()
    report = {'platform': platform.platform(), 'python': platform.python_version(),
              'filesystem': attempt(lambda: filesystem_name(root)), 'probe_folder': directory.name}
    local = local_folder('familyfoto-probe-local-')
    try:
        for name, function in (('hard_link', hard_link), ('rename_refuses_existing', rename_refuses_existing),
                               ('rename_exclusive', rename_exclusive), ('exclusive_create', exclusive_create),
                               ('permissions', permissions), ('identities', identities), ('timestamps', timestamps),
                               ('names', names), ('symlink', symlink), ('sidecars', sidecars),
                               ('private_folder_access', private_folder_access),
                               ('directory_sync', directory_sync), ('locks', locks), ('sqlite', sqlite_database)):
            report[name] = attempt(lambda function=function: function(directory))
        report['directory_identity'] = attempt(lambda: list(directory_identity(directory)))
        report['volume_identity'] = attempt(lambda: volume_identity(directory))
        report['real_code'] = real_code(directory, local)
        report['entries_left_by_system'] = attempt(lambda: sorted(
            name for name in os.listdir(directory) if name.startswith(('._', '.DS_Store'))))
    finally:
        shutil.rmtree(local, ignore_errors=True)
        if not keep:
            report['cleanup_failures'] = remove_folder(directory)
    report['removed'] = not directory.exists()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', help='Existing folder on the disk to measure')
    parser.add_argument('--keep', action='store_true', help='Leave the probe folder for inspection')
    parser.add_argument('--prepare-remount', action='store_true',
                        help='Leave interrupted work under --root, then eject and reconnect the disk')
    parser.add_argument('--check-remount', help='The prepared folder, after the disk was reconnected')
    arguments = parser.parse_args()
    if arguments.check_remount:
        result = check_remount(arguments.check_remount)
    elif not arguments.root:
        parser.error('--root is required')
    elif arguments.prepare_remount:
        result = prepare_remount(arguments.root)
    else:
        result = probe(arguments.root, arguments.keep)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == '__main__':
    main()
