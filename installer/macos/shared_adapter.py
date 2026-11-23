"""Mac adapter: prepare local inputs, then hand over to the existing Mac installer.

The shared engine has already acquired and verified every payload (installer/common). This
adapter materializes the wheels and locks, stages the application, unpacks the bundled Python,
PostgreSQL and FFmpeg, and runs `installer/macos/install.sh` with FAMILYFOTO_PAYLOAD_RECEIPT
set. The Mac setup records (.macos-setup.json, runtime/macos-*.json, the restore journal)
remain the only provisioning journal. It runs as the signed-in user and never asks for sudo.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer.common.acquire import extract_zip, file_digest, plain_path, verified  # noqa: E402
from installer.common.manifest import os_version_tuple  # noqa: E402
from installer.common.materialize import materialize  # noqa: E402
from installer.common.stage import stage_application as _stage  # noqa: E402
from installer.common.state import atomic_json  # noqa: E402
from installer.common.workflow import read_manifest  # noqa: E402
from installer.macos.bundled import EXECUTABLES, FOLDER, program_dir, record_receipt  # noqa: E402

PLATFORM = 'macos-arm64'
# Owner decision (2026-11-22): current macOS only, no legacy support. Every Mac release requires
# macOS 27 or newer; the audit refuses any bundled program that needs more than a release declares.
MINIMUM_MACOS = '27.0'


def require_minimum_os(manifest):
    """A Mac release must declare a minimum_os the bundled wheels actually support."""
    declared = manifest.document.get('minimum_os')
    if declared is None or os_version_tuple(declared) < os_version_tuple(MINIMUM_MACOS):
        raise ValueError(f'Mac releases must declare minimum_os {MINIMUM_MACOS} or newer')


def generated_bytecode(relative):
    """Python writes these caches next to its own library as it runs; nothing else may appear."""
    return relative.suffix == '.pyc' and '__pycache__' in relative.parts


def verify_tree(archive, target):
    """The unpacked folder is exactly its archive: same file contents, nothing added, no links.

    The archive itself was verified against the release manifest, so comparing each file's
    SHA-256 with its archive entry authenticates what will actually run.
    """
    target = plain_path(target)
    expected = {}
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            if entry.is_dir():
                continue
            digest = hashlib.sha256()
            with package.open(entry) as stream:
                while block := stream.read(1 << 20):
                    digest.update(block)
            expected[Path(entry.filename.rstrip('/'))] = digest.hexdigest()
    for path in sorted(target.rglob('*')):
        relative = path.relative_to(target)
        if path.is_symlink():
            raise ValueError(f'Unexpected link in bundled program: {target.name}/{relative.as_posix()}')
        if path.is_dir():
            continue
        if relative not in expected:
            if generated_bytecode(relative):
                continue
            raise ValueError(f'Unexpected file in bundled program: {target.name}/{relative.as_posix()}')
        if generated_bytecode(relative):
            expected.pop(relative)  # the interpreter may rewrite a shipped cache; not executed as-is
            continue
        if file_digest(path) != expected.pop(relative):
            raise ValueError(f'Bundled program files changed or incomplete: {target.name}/{relative.as_posix()}')
    missing = sorted(name.as_posix() for name in expected if not generated_bytecode(name))
    if missing:
        raise ValueError(f'Bundled program files changed or incomplete: {target.name}/{missing[0]}')


def stage_application(manifest, receipt, destination):
    return _stage(manifest, receipt, destination, PLATFORM)


def unpack_runtimes(manifest, receipt, root):
    """Unpack each bundled program once, bound to the exact payload that produced it."""
    root = plain_path(root)
    selected = []
    for key, path in sorted(receipt['paths'].items()):
        item = manifest.components[key]
        if item.get('runtime_role'):
            archive = plain_path(path)
            # Check every archive before unpacking any, so a bad download changes nothing.
            if not verified(archive, item):
                raise ValueError('Bundled program changed after download: ' + key)
            selected.append((key, item, archive))
    unpacked = {}
    for key, item, archive in selected:
        role = item['runtime_role']
        folder = root / FOLDER
        target = folder / role
        marker = folder / (role + '.json')
        record = {'schema_version': 1, 'component_id': key, 'sha256': item['sha256']}
        current = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else None
        if current == dict(record, state='complete'):
            verify_tree(archive, target)  # resume: catch a partial or altered tree, not just the archive
        elif current == dict(record, state='unpacking') or (current is None and not target.exists()):
            # Ours and unfinished (an abrupt stop mid-unpack), or not started: unpack afresh.
            folder.mkdir(mode=0o700, exist_ok=True)
            atomic_json(marker, dict(record, state='unpacking'))
            if target.exists():
                shutil.rmtree(plain_path(target))
            extract_zip(archive, target, item['installed_size_estimate'])
            verify_tree(archive, target)
            atomic_json(marker, dict(record, state='complete'))
        else:
            raise ValueError('Existing bundled ' + role + ' is not from this release; use a fresh installation folder')
        unpacked[role] = str(program_dir(role, root))
    missing = sorted(set(EXECUTABLES) - set(unpacked))
    if missing:
        raise ValueError('Release lacks bundled ' + ', '.join(missing))
    return unpacked


def handoff_command(root, mode):
    return ['/bin/bash', str(Path(root) / 'installer/macos/install.sh'), mode]


def provision(manifest, receipt_path, root, os_version=None):
    """Materialize, stage, unpack and record; returns the root ready for install.sh."""
    require_minimum_os(manifest)
    receipt = materialize(manifest, receipt_path, PLATFORM, os_version)
    root = plain_path(root)
    stage_application(manifest, receipt, root)
    unpack_runtimes(manifest, receipt, root)
    # Lets `bash installer/macos/install.sh restore` resume later from a fresh shell.
    record_receipt(root, receipt_path, manifest.digest)
    if not (root / 'installer/macos/install.sh').is_file():
        raise ValueError('Verified application release must be staged at --root first')
    return root


def handoff(root, mode, receipt_path, extra_env=None):
    """Run the existing Mac installer with only local inputs; returns its exit code."""
    # Passwords stay in the setup window and its private pipes, never in argv or this environment.
    env = {**os.environ, **(extra_env or {}), 'FAMILYFOTO_PAYLOAD_RECEIPT': os.path.abspath(receipt_path)}
    return subprocess.run(handoff_command(root, mode), cwd=root, env=env, check=False).returncode


def require_host():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Mac provisioning requires Apple Silicon macOS')
    if os.geteuid() == 0:
        raise SystemExit('Run as the signed-in user, not sudo')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--manifest-sha256', required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--mode', choices=['install', 'restore'], required=True)
    p.add_argument('--os-version', help='macOS product version from the launcher (ProcessInfo)')
    a = p.parse_args(argv)
    require_host()
    manifest = read_manifest(a.manifest, a.manifest_sha256)
    root = provision(manifest, a.receipt, a.root, a.os_version)
    raise SystemExit(handoff(root, a.mode, a.receipt))


if __name__ == '__main__':
    main()
