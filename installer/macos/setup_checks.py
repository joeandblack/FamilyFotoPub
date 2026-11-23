"""Read-only setup checks. Password authentication is an explicit separate action."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.backup import passwords
from installer.macos.restore_input import password_problem, needs_backup
from app.service.instance import NAME
from installer.macos import bundled, prepare


def port_free(port):
    try:
        prepare.require_free_ports([port])
        return True
    except OSError:
        return False


def loaded_instances():
    """Read only the installation name for each loaded per-folder launchd job."""
    jobs = subprocess.check_output(['/bin/launchctl', 'list'], text=True)
    names = set()
    for line in jobs.splitlines():
        label = line.split()[-1]
        if not label.startswith('local.familyfoto.'):
            continue
        names.add('')
        detail = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                                capture_output=True, text=True).stdout
        for field in detail.splitlines():
            if field.strip().startswith('working directory = '):
                root = Path(field.strip().split(' = ', 1)[1])
                try:
                    names.add(json.loads((root / 'config/local.json').read_bytes()).get('instance', ''))
                except (OSError, ValueError):
                    pass
    return names


def layout(names):
    from installer.macos.workflow import people_terms
    ports = [5433, 4341, 54341]
    name = ''
    if names or not all(map(port_free, ports)):
        number = 1
        name = 'restore'
        while name in names:
            number += 1
            name = f'restore-{number}'
        ports = [next((p for p in range(start, end) if port_free(p)), 0)
                 for start, end in [(5434, 5500), (4342, 4400), (54344, 54400)]]
    return dict(zip(('database', 'web', 'peoplePort'), map(str, ports)), instance=name, terms=people_terms())


def disk_protected(parent):
    # diskutil needs the mount point, not an arbitrary directory on the volume.
    mount = parent
    while not mount.is_mount() and mount != mount.parent:
        mount = mount.parent
    result = subprocess.check_output(['/usr/sbin/diskutil', 'info', '-plist', str(mount)])
    info = plistlib.loads(result)
    return info.get('GlobalPermissionsEnabled', True) and info.get('FilesystemType', '').lower() in ('apfs', 'hfs')


def copy_size(backup):
    # Older backups already record the inventory's byte total in the completed media tree.
    for relative, field in [('capture-timing.json', 'media_bytes'), ('media-copy/TREE-COMPLETE.json', 'bytes')]:
        try:
            record = json.loads((backup / relative).read_bytes())
            size = record[field]
            if type(size) is int and size >= 0:
                return size
        except (OSError, KeyError, ValueError, TypeError):
            continue
    return None


def validate(a):
    errors = {}
    try:
        prepare.require_fresh(ROOT)
        if shutil.disk_usage(ROOT).free < bundled.WORKING_SPACE:
            errors['layout'] = f'The installation disk needs {bundled.WORKING_SPACE // 1024**3} GB of free space.'
        ports = [int(a[k]) for k in ('database', 'web', 'peoplePort')]
        if (len(set(ports)) != 3 or min(ports) < 1024 or max(ports) > 65535 or ports[2] < 49152
                or not all(map(port_free, ports))):
            errors['layout'] = 'Choose three available ports. The People port must be 49152–65535.'
        names = loaded_instances()
        if names and a['instance'] in names:
            errors['layout'] = 'Choose a unique name for this additional installation.'
        if a['instance'] and not NAME.fullmatch(a['instance']):
            errors['layout'] = 'Use 1–24 lowercase letters, digits or hyphens for the installation name.'
    except (OSError, ValueError) as error:
        errors['layout'] = str(error)
    if not a['restore']:
        return errors
    backup = Path(a['backup']).resolve()
    if not a['backup'] or not (backup / 'catalog').is_dir() or not (backup / 'library/complete.fernet').is_file():
        errors['backup'] = 'Choose a finished backup containing catalog and library/complete.fernet.'
    else:
        try:
            passwords.read_context(backup)
        except passwords.PasswordProblem as error:
            errors['backup'] = str(error)
    parent = Path(a['parent']).resolve()
    folder = parent / 'FFLibrary'
    if not a['parent'] or not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        errors['parent'] = 'Choose a writable folder for FFLibrary.'
    elif folder.exists() or folder.is_symlink() or folder.is_relative_to(backup) or (backup.exists() and any(part.samefile(backup) for part in (parent, *parent.parents))):
        errors['parent'] = 'Choose a folder outside the backup where FFLibrary does not exist.'
    elif 'backup' not in errors:
        if not a['copy'] and parent.stat().st_dev != backup.stat().st_dev:
            errors['parent'] = 'In place needs a folder on the backup disk.'
        elif a['copy']:
            size = copy_size(backup)
            if size is None:
                errors['parent'] = 'Cannot verify copy space. Reconnect a complete backup with its media inventory.'
            elif shutil.disk_usage(parent).free < (size * 105 + 99) // 100 + 1024**3:
                errors['parent'] = 'The disk needs more free space for this copy.'
        if not disk_protected(parent):
            errors['unprotected'] = 'Anyone with this disk can read the photos and videos.'
    return errors


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'layout':
            result = layout(loaded_instances())
        else:
            raw = sys.stdin.buffer.read(16385)
            if len(raw) > 16384:
                raise ValueError('Input too large')
            answers = json.loads(raw)
            if sys.argv[1] == 'resume':
                result = {'needsPassword': 'true' if needs_backup(ROOT) else 'false'}
            elif sys.argv[1] == 'password':
                problem = password_problem(Path(answers['backup']), answers['password'])
                result = {'password': problem} if problem else {}
            else:
                result = validate(answers)
        print(json.dumps(result))
    except passwords.PasswordProblem as error:
        print(json.dumps({'resume': str(error)}))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Never include input or exception details: this pipe can carry a password.
        print(json.dumps({'layout': 'Could not check the selected backup or installation. Reconnect the disk and try again.'}))
