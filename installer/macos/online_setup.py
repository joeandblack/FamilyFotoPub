"""Mac launcher engine: admission, downloads with progress, then the Mac adapter.

FamilyFoto Setup.app (installer/macos/launcher) runs this with the bootstrap Python after the
user accepted the agreement and chose an installation folder. It prints `@@ {json}` progress
lines (installer/common/README.md, "Events") for the app's window, then hands over to
`installer/macos/install.sh install`, which opens the release's prebuilt setup window.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from installer.common.acquire import plain_path  # noqa: E402
from installer.common.workflow import prepare, read_manifest  # noqa: E402
from installer.macos import shared_adapter  # noqa: E402
from installer.common.stage import MARKER  # noqa: E402
from installer.macos.bundled import WORKING_SPACE  # noqa: E402

PLATFORM = shared_adapter.PLATFORM


def read_choices(path):
    """The app's answers. Validated here too: the engine never trusts the UI blindly."""
    choices = json.loads(Path(path).read_text(encoding='utf-8'))
    root = Path(choices['root'])
    if not root.is_absolute() or '\n' in str(root):
        raise ValueError('Choose an absolute installation folder')
    if not isinstance(choices.get('people'), bool) or not isinstance(choices.get('agreement_digest'), str):
        raise ValueError('Invalid setup choices')
    return root, choices['people'], choices['agreement_digest']


def admit(manifest, root, features, os_version):
    """Refuse an unusable folder or too little space before anything is downloaded."""
    selected = manifest.plan(features, PLATFORM, os_version)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('Choose an installation folder whose path has no symbolic links')
    if root.exists():
        marker = root / MARKER
        owned = marker.is_file() and json.loads(marker.read_text(encoding='utf-8')).get('manifest_digest') == manifest.digest
        if not owned and any(entry.name != '.DS_Store' for entry in root.iterdir()):
            raise ValueError('The installation folder already contains files. Choose an empty or new folder; nothing was downloaded.')
    if not root.parent.is_dir():
        raise ValueError('Choose an installation folder inside an existing folder')
    # The installation itself, plus the working space the setup window insists on afterwards:
    # checked here so nobody downloads everything only to be refused at the next step.
    installed = sum(item['installed_size_estimate'] for item in selected)
    required = installed + WORKING_SPACE
    free = shutil.disk_usage(root.parent).free
    if free < required:
        gb = lambda n: f'{n / 1024**3:.1f} GB'  # noqa: E731
        raise OSError(f'Not enough free space on this disk: FamilyFoto needs {gb(required)} '
                      f'({gb(installed)} to install plus {WORKING_SPACE // 1024**3} GB to work in) '
                      f'and {gb(free)} is free. Nothing was downloaded.')
    return selected


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--descriptor', type=Path, required=True)
    p.add_argument('--attempt', type=Path, required=True)
    p.add_argument('--choices', type=Path, required=True)
    p.add_argument('--bundle', type=Path)
    p.add_argument('--os-version', required=True, help='macOS product version from ProcessInfo')
    p.add_argument('--prepare-only', action='store_true', help='stop after staging (tests, rehearsals)')
    a = p.parse_args(argv)
    a.attempt = Path(os.path.abspath(a.attempt))
    shared_adapter.require_host()
    config = json.loads(a.descriptor.read_text(encoding='utf-8'))
    if config.get('schema_version') != 1 or config.get('mode') not in ('online', 'offline'):
        raise ValueError('Unknown bootstrap descriptor')
    manifest = read_manifest(ENGINE / 'installer-manifest.json', config['manifest_sha256'])
    agreement = plain_path(ENGINE / manifest.document['agreement_path']).read_bytes()
    root, people, accepted = read_choices(a.choices)
    features = ['core'] + (['people'] if people else [])
    shared_adapter.require_minimum_os(manifest)
    admit(manifest, root, features, a.os_version)
    cancel = a.attempt / 'cancel.requested'
    cancel.unlink(missing_ok=True)
    prepare(manifest, platform=PLATFORM, features=features, agreement=agreement, accepted_digest=accepted,
            directory=a.attempt, mode=config['mode'], bundle=a.bundle, stream=sys.stdout,
            cancelled=cancel.exists, install_root=root, os_version=a.os_version)
    receipt = a.attempt / 'prepared.json'
    shared_adapter.provision(manifest, receipt, root, a.os_version)
    print('Downloads verified. Opening FamilyFoto Setup…', flush=True)
    if a.prepare_only:
        return 0
    env = {'FAMILYFOTO_LAUNCHER': '1'}
    if os.environ.get('FAMILYFOTO_RESTORE_BACKUP'):
        env['FAMILYFOTO_RESTORE_BACKUP'] = os.environ['FAMILYFOTO_RESTORE_BACKUP']
    return shared_adapter.handoff(root, 'install', receipt, env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, InterruptedError) as error:
        print('Setup stopped: ' + str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)
