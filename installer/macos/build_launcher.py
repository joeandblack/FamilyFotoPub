"""Release-side Mac builds for the shared installer. Run on an Apple Silicon build Mac.

  engine         installer engine ZIP the launcher downloads (code + pinned manifest + agreement)
  setup-window   the compiled setup window as a link-free `setup-window` runtime ZIP
  python-runtime a python-build-standalone install_only archive as a link-free runtime ZIP
  app            FamilyFoto Setup.app with an embedded bootstrap.json, ad-hoc signed
  dmg            a compressed disk image containing the app

Nothing here downloads, publishes or installs. Outputs must not exist yet.
"""
import argparse
import json
import plistlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer.common.acquire import file_digest  # noqa: E402
from installer.common.manifest import Manifest, digest_string, os_version_tuple, safe_relative, sha256  # noqa: E402

APP_NAME = 'FamilyFoto Setup'
# swiftc otherwise marks binaries as needing the build Mac's own macOS; older Macs then refuse them.
DEFAULT_MINIMUM_OS = '27.0'  # installer/macos/shared_adapter.MINIMUM_MACOS


def swift_target(minimum_os):
    os_version_tuple(minimum_os)
    return ['-target', f'arm64-apple-macos{minimum_os}']
BUNDLE_ID = 'org.familyfoto.setup'
ENGINE_FILES = ['app/__init__.py', 'app/migration/__init__.py', 'app/migration/progress_lines.py',
                'installer/macos/online_setup.py', 'installer/macos/shared_adapter.py',
                'installer/macos/bundled.py']


def add_file(archive, name, data, executable=False):
    """Deterministic, link-free entries; the executable bit is what extract_zip keeps."""
    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def build_engine(manifest_bytes, payload_root, output):
    manifest = Manifest(manifest_bytes, sha256(manifest_bytes))
    if manifest.document['platform'] != 'macos-arm64':
        raise ValueError('The Mac engine needs a macos-arm64 release manifest')
    agreement = Path(payload_root) / str(safe_relative(manifest.document['agreement_path']))
    if file_digest(agreement) != manifest.document['agreement_sha256']:
        raise ValueError('Agreement changed after manifest creation')
    files = sorted({*ENGINE_FILES, *(p.relative_to(ROOT).as_posix() for p in (ROOT / 'installer/common').glob('*.py'))})
    with zipfile.ZipFile(output, 'x') as archive:
        for name in files:
            add_file(archive, name, (ROOT / name).read_bytes())
        add_file(archive, 'installer-manifest.json', manifest_bytes)
        add_file(archive, manifest.document['agreement_path'], agreement.read_bytes())


def adhoc_sign(path):
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', str(path)], check=True)


def build_setup_window(output, minimum_os=DEFAULT_MINIMUM_OS):
    with tempfile.TemporaryDirectory() as folder:
        binary = Path(folder) / 'familyfoto-setup'
        subprocess.run(['/usr/bin/swiftc', '-parse-as-library', '-O', *swift_target(minimum_os),
                        str(ROOT / 'installer/macos/SetupWindow.swift'), '-o', str(binary)], check=True)
        adhoc_sign(binary)
        with zipfile.ZipFile(output, 'x') as archive:
            add_file(archive, 'bin/familyfoto-setup', binary.read_bytes(), executable=True)


def link_free_tree(source):
    """Regular files only, with in-tree links replaced by copies of their targets."""
    source = Path(source).resolve()
    files = {}
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(source):
                raise ValueError('Runtime link escapes its tree: ' + relative)
            if target.is_dir():
                continue  # the real directory is archived under its own name
            path = target
        elif path.is_dir():
            continue
        files[relative] = path
    return files


def build_python_runtime(archive_path, output):
    with tempfile.TemporaryDirectory() as folder:
        with tarfile.open(archive_path) as tar:
            try:
                tar.extractall(folder, filter='data')
            except tarfile.FilterError as error:  # absolute or escaping links, devices, unsafe paths
                raise ValueError(f'Runtime link escapes its tree: {error}') from error
        top = Path(folder) / 'python'
        interpreter = top / 'bin/python3.12'
        if not interpreter.exists():
            raise ValueError('Expected a python-build-standalone install_only archive (python/bin/python3.12)')
        with zipfile.ZipFile(output, 'x') as archive:
            for name, path in link_free_tree(top).items():
                add_file(archive, name, path.read_bytes(), executable=bool(path.stat().st_mode & 0o111))


def validate_descriptor(doc):
    if doc.get('schema_version') != 1 or doc.get('mode') not in ('online', 'offline'):
        raise ValueError('Unknown bootstrap descriptor')
    digest_string(doc['manifest_sha256'])
    os_version_tuple(doc['minimum_os'])
    if not doc.get('bootstrap_notices') or not isinstance(doc.get('release_id'), str):
        raise ValueError('Release identity and bootstrap notices are required')
    for key in ('python', 'engine'):
        digest_string(doc[key]['sha256'])
        safe_relative(doc[key]['bundle_path'])
        if not doc[key]['url'].startswith('https://'):
            raise ValueError('Bootstrap requires HTTPS URLs')
    return doc


def build_app(descriptor_path, output, bundle=None):
    output = Path(output)
    if output.exists():
        raise ValueError('Output already exists')
    doc = validate_descriptor(json.loads(Path(descriptor_path).read_text(encoding='utf-8')))
    contents = output / 'Contents'
    (contents / 'MacOS').mkdir(parents=True)
    resources = contents / 'Resources'
    resources.mkdir()
    subprocess.run(['/usr/bin/swiftc', '-parse-as-library', '-O', *swift_target(doc['minimum_os']),
                    str(ROOT / 'installer/macos/launcher/Launcher.swift'), '-o', str(contents / 'MacOS' / APP_NAME)], check=True)
    info = {'CFBundleName': APP_NAME, 'CFBundleDisplayName': APP_NAME, 'CFBundleExecutable': APP_NAME,
            'CFBundleIdentifier': BUNDLE_ID, 'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
            'CFBundleShortVersionString': '1.0', 'LSMinimumSystemVersion': doc['minimum_os'],
            'LSArchitecturePriority': ['arm64'], 'NSHighResolutionCapable': True}
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    shutil.copyfile(descriptor_path, resources / 'bootstrap.json')
    if bundle is not None:
        if doc['mode'] != 'offline':
            raise ValueError('Only an offline descriptor carries a payload bundle')
        if not (Path(bundle) / 'COMPLETE').is_file():
            raise ValueError('The offline bundle is incomplete (no COMPLETE marker)')
        shutil.copytree(bundle, resources / 'bundle', symlinks=False)
    adhoc_sign(output)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(output)], check=True)


def build_dmg(app, output):
    if Path(output).exists():
        raise ValueError('Output already exists')
    subprocess.run(['/usr/bin/hdiutil', 'create', '-quiet', '-volname', APP_NAME, '-srcfolder', str(app),
                    '-format', 'UDZO', str(output)], check=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['engine', 'setup-window', 'python-runtime', 'app', 'dmg'])
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--manifest', type=Path)
    p.add_argument('--payload-root', type=Path)
    p.add_argument('--archive', type=Path)
    p.add_argument('--descriptor', type=Path)
    p.add_argument('--bundle', type=Path)
    p.add_argument('--app', type=Path)
    p.add_argument('--minimum-os', default=DEFAULT_MINIMUM_OS, help='setup-window: oldest macOS it must run on')
    a = p.parse_args(argv)
    if a.command == 'engine':
        build_engine(a.manifest.read_bytes(), a.payload_root, a.output)
    elif a.command == 'setup-window':
        build_setup_window(a.output, a.minimum_os)
    elif a.command == 'python-runtime':
        build_python_runtime(a.archive, a.output)
    elif a.command == 'app':
        build_app(a.descriptor, a.output, a.bundle)
    else:
        build_dmg(a.app, a.output)


if __name__ == '__main__':
    main()
