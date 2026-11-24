"""Release-side: assemble and audit a Mac release of the shared installer.

  assemble   recipe JSON -> payload tree (bundle_path layout), manifest.json, assets.json
  audit      refuse a manifest/payload that is incomplete, mis-sized or missing sources
  descriptor bootstrap.json for FamilyFoto Setup.app (online or offline)
  bundle     offline payload bundle for the app's Resources/bundle (installer/common/bundle.py)

The recipe names local, already-built inputs; nothing here downloads or publishes:

  {"release_id": "2026.12.1", "source_commit": "<40 hex>", "minimum_os": "27.0",
   "agreement": "agreement.txt",            (optional; default installer/legal/agreement.txt)
   "application": "FamilyFoto-2026.12.1.zip",                 (installer/release.py build)
   "runtimes": {"python": "python.zip", "postgres": "postgres.zip",
                "ffmpeg": "ffmpeg.zip", "setup-window": "setup-window.zip"},
   "locks": {"core": "installer/locks/core-macos-arm64.txt", "password": ..., "people-view": ...,
             "insightface": ..., "facenet": ...},
   "wheels": "wheelhouse/",                                   (every wheel the locks pin)
   "models": {"insightface": "buffalo_l.zip", "facenet": "20180402-114759-vggface2.pt"},
   "licenses": {"application": ["FamilyFoto"], "python": ["PSF-2.0"], "wheel:insightface": ["MIT"], ...},
   "sources": "sources/",    (published with the release; `audit` lists what it must hold)
   "windows": true}          (optional, default true: the Windows setup is published under the
                              same release_id, so new backups name it; false for a Mac-only release)

Assets are published flat: `bundle_path` with `/` replaced by `--`, under RELEASE_BASE.
"""
import argparse
import email
import hashlib
import json
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer.common.bundle import build as build_bundle  # noqa: E402
from installer.common.manifest import (PYPI, RELEASE_BASE, SETUP_ASSETS, Manifest, os_version_tuple,  # noqa: E402
                                       safe_relative, sha256)
from installer.macos.build_payloads import MACHO, macho_minimum_bytes  # noqa: E402
from installer.macos.bundled import EXECUTABLES  # noqa: E402
from installer.macos.shared_adapter import MINIMUM_MACOS  # noqa: E402

PLATFORM = 'macos-arm64'
CORE_LOCKS = ('core', 'password', 'people-view')
PEOPLE_LOCKS = ('insightface', 'facenet')
MODELS = json.loads((ROOT / 'face_detect/config/research-models.json').read_text(encoding='utf-8'))
AGREEMENT = ROOT / 'installer/legal/agreement.txt'
# Licences whose terms require the source to be offered with the binaries (the agreement promises it).
COPYLEFT = re.compile(r'GPL|General Public License|MPL|Mozilla Public|EPL|Eclipse Public|CDDL|EUPL', re.I)


def normalized(name):
    return re.sub('[-_.]+', '-', name).lower()


def missing_sources(manifest, payload_root):
    """What the release must publish as source but sources/ (beside payload/) lacks.

    Every Homebrew formula in the native payloads needs its source archive (checked against its
    SHA-256) and its recipe as sources/homebrew/<formula>.rb; every wheel under a copyleft licence
    needs its source distribution, sources/<name>-<version>.tar.gz or .zip."""
    sources = Path(payload_root).parent / 'sources'
    present = {normalized(p.name): p for p in sources.rglob('*') if p.is_file()} if sources.is_dir() else {}
    problems = []
    for item in manifest.components.values():
        path = Path(payload_root) / str(safe_relative(item['bundle_path']))
        if item.get('runtime_role') in ('postgres', 'ffmpeg') and path.is_file():
            try:
                info = json.loads(zipfile.ZipFile(path).read('payload-info.json'))
            except (zipfile.BadZipFile, KeyError, ValueError):
                continue  # a damaged payload is already reported as differing from the manifest
            for formula, entry in sorted(info.get('formulas', {}).items()):
                if entry.get('source_url'):
                    archive = Path(urlsplit(entry['source_url']).path).name
                    found = present.get(normalized(archive))
                    if not found or (entry.get('source_sha256') and file_sha256(found) != entry['source_sha256']):
                        problems.append(f"{item['id']}: sources/ lacks {archive}, the source of {formula} {entry['version']}")
                if not (sources / 'homebrew' / f'{formula}.rb').is_file():
                    problems.append(f"{item['id']}: sources/homebrew/ lacks {formula}.rb, its Homebrew recipe")
        elif item['bundle_path'].endswith('.whl') and any(COPYLEFT.search(i) for i in item['license_ids']):
            name, version = Path(item['bundle_path']).name.split('-')[:2]
            stem = normalized(f'{name}-{version}')
            # Exactly the archive, never a sidecar such as .tar.gz.sha256, and one that opens.
            archive = present.get(stem + '-tar-gz') or present.get(stem + '-zip')
            if not (archive and (tarfile.is_tarfile(archive) or zipfile.is_zipfile(archive))):
                problems.append(f"{item['id']}: sources/ lacks the source distribution of {name} {version} "
                                f"({', '.join(item['license_ids'])})")
    return problems


def model_digest(provider):
    """The fingerprint face_detect/cli/setup.py looks up in the receipt for this provider."""
    entry = MODELS[provider]
    return entry.get('archive_sha256') or entry['assets']['recognizer']['sha256']


def file_sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def uncompressed(path):
    return sum(i.file_size for i in zipfile.ZipFile(path).infolist()) if zipfile.is_zipfile(path) else Path(path).stat().st_size


def asset_name(bundle_path):
    return bundle_path.replace('/', '--')


def wheel_license(path):
    """An SPDX-ish license from wheel metadata: License-Expression, classifier or License."""
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
        meta = email.message_from_bytes(archive.read(name))
    if meta.get('License-Expression'):
        return [meta['License-Expression'].strip()]
    classifiers = [c.split('::')[-1].strip() for c in meta.get_all('Classifier') or [] if c.startswith('License ::')]
    if classifiers:
        return classifiers
    text = (meta.get('License') or '').strip().splitlines()
    return [text[0][:60]] if text and text[0] and text[0].upper() != 'UNKNOWN' else []


def lock_pins(path):
    """{sha256: 'name==version'} for every `--hash=sha256:` pin in a lock file."""
    pins = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.startswith('#'):
            for digest in re.findall(r'--hash=sha256:([0-9a-f]{64})', line):
                pins[digest] = line.split()[0]
    return pins


def index_lock_problems(item, path):
    """A lock installed from PyPI must pin every requirement by hash, since nothing of it ships."""
    if not item.get('wheel_index'):
        return []
    unpinned = [line.split()[0] for line in Path(path).read_text(encoding='utf-8').splitlines()
                if line.strip() and not line.startswith('#') and '--hash=sha256:' not in line]
    return [f"{item['id']}: installed from PyPI, so every requirement needs a --hash ({', '.join(unpinned)})"] if unpinned else []


def runtime_minimum(archive):
    """(version, entry name) of the newest macOS any Mach-O in a runtime ZIP needs.

    Read in memory from each entry's Mach-O header: nothing from the archive is written to
    disk (its names are never trusted as paths), and it works on every operating system.
    """
    worst = None
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            if entry.is_dir():
                continue
            with package.open(entry) as stream:
                if stream.read(4) not in MACHO:
                    continue
            version = macho_minimum_bytes(package.read(entry))
            if version and (worst is None or os_version_tuple(version) > os_version_tuple(worst[0])):
                worst = (version, entry.filename)
    return worst


def assemble(recipe_path, output):
    recipe_path, output = Path(recipe_path), Path(output)
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    base = recipe_path.parent
    path = lambda value: (base / value).resolve()  # noqa: E731
    licenses = recipe.get('licenses', {})
    if output.exists():
        raise ValueError('Output already exists')
    payload = output / 'payload'
    payload.mkdir(parents=True)
    components, features = [], {'core': [], 'people': []}
    release_url = RELEASE_BASE + '/' + quote(recipe['release_id'], safe='')

    def add(cid, source, bundle_path, feature, license_ids, **extra):
        target = payload / str(safe_relative(bundle_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        components.append(dict(id=cid, bundle_path=bundle_path, byte_size=target.stat().st_size,
                               installed_size_estimate=uncompressed(target), sha256=file_sha256(target),
                               url=release_url + '/' + asset_name(bundle_path), dependencies=[],
                               license_ids=list(license_ids), **extra))
        features[feature].append(cid)

    add('application', path(recipe['application']), 'application/application.zip', 'core',
        licenses.get('application', ['FamilyFoto']))
    for role in EXECUTABLES:
        add('runtime-' + role, path(recipe['runtimes'][role]), f'runtime/{role}.zip', 'core',
            licenses.get(role, []), runtime_role=role)
    wheelhouse = {file_sha256(w): w for w in sorted(path(recipe['wheels']).glob('*.whl'))}
    placed = set()
    for role in (*CORE_LOCKS, *PEOPLE_LOCKS):
        feature = 'core' if role in CORE_LOCKS else 'people'
        lock = path(recipe['locks'][role])
        if role in PEOPLE_LOCKS:
            # Not shipped: setup installs People's provider wheels from PyPI, each pinned by this lock.
            add('lock-' + role, lock, f'locks/{role}.txt', feature, ['FamilyFoto'], lock_role=role, wheel_index=PYPI)
            continue
        add('lock-' + role, lock, f'locks/{role}.txt', feature, ['FamilyFoto'], lock_role=role)
        for digest, pin in sorted(lock_pins(lock).items()):
            if digest in placed:
                continue
            if digest not in wheelhouse:
                raise ValueError(f'The wheelhouse lacks {pin} ({role} lock)')
            wheel = wheelhouse[digest]
            # The recipe states a license where a wheel's metadata is silent (e.g. "wheel:insightface").
            name = re.sub('[-_.]+', '-', wheel.name.split('-')[0]).lower()
            add('wheel-' + re.sub('[^a-z0-9]+', '-', wheel.name.lower())[:100].strip('-'), wheel,
                'wheels/' + wheel.name, feature, licenses.get('wheel:' + name) or wheel_license(wheel))
            placed.add(digest)
    for provider in PEOPLE_LOCKS:
        source = path(recipe['models'][provider])
        add('model-' + provider, source, f'models/{provider}/{source.name}', 'people',
            licenses.get('model-' + provider, []), model_provider=provider,
            model_identity=MODELS[provider]['model'])
    # The FamilyFoto Terms and Third-Party Notices kept with the code, unless the recipe names others.
    agreement = path(recipe['agreement']) if 'agreement' in recipe else AGREEMENT
    if 'sources' in recipe:  # published beside the payload; the audit checks what it must contain
        shutil.copytree(path(recipe['sources']), output / 'sources')
    (payload / 'legal').mkdir()
    shutil.copyfile(agreement, payload / 'legal' / agreement.name)
    doc = dict(schema_version=1, installer_version=1, release_id=recipe['release_id'],
               source_commit=recipe['source_commit'], platform=PLATFORM,
               minimum_os=recipe.get('minimum_os', MINIMUM_MACOS),
               setup_url=release_url + '/' + SETUP_ASSETS[PLATFORM],
               agreement_path='legal/' + agreement.name, agreement_sha256=file_sha256(agreement),
               components=components, features=features)
    if recipe.get('windows', True):
        # Both platforms ship under one release_id, so a Mac backup names the matching Windows setup.
        doc['windows_handover'] = dict(release_id=recipe['release_id'],
                                       setup_url=release_url + '/' + SETUP_ASSETS['windows-x64'])
    raw = json.dumps(doc, indent=1, sort_keys=True).encode()
    (output / 'manifest.json').write_bytes(raw)
    (output / 'assets.json').write_text(json.dumps(
        {asset_name(c['bundle_path']): c['bundle_path'] for c in components}, indent=1, sort_keys=True))
    return Manifest(raw, sha256(raw))  # run `audit` before publishing; `bundle` refuses without it


def audit(manifest, payload_root):
    """Refuse an incomplete or inconsistent Mac release before anything is published."""
    doc, payload_root = manifest.document, Path(payload_root)
    problems = []
    if doc['platform'] != PLATFORM:
        problems.append('not a macos-arm64 release')
    minimum = doc.get('minimum_os')
    if not minimum or os_version_tuple(minimum) < os_version_tuple(MINIMUM_MACOS):
        problems.append(f'minimum_os must be {MINIMUM_MACOS} or newer')
    if not doc.get('setup_url'):
        problems.append('setup_url (the DMG link written into new backups) is missing')
    core = manifest.plan(['core'], PLATFORM, minimum or MINIMUM_MACOS)
    roles = {c.get('runtime_role') for c in core}
    problems += [f'core lacks the bundled {r}' for r in EXECUTABLES if r not in roles]
    if 'application' not in {c['id'] for c in core}:
        problems.append('core lacks the application')
    locks = {c.get('lock_role') for c in core}
    problems += [f'core lacks the {r} lock' for r in CORE_LOCKS if r not in locks]
    if 'people' in doc['features']:
        people = manifest.plan(['core', 'people'], PLATFORM, minimum or MINIMUM_MACOS)
        locks = {c.get('lock_role') for c in people}
        problems += [f'People lacks the {r} lock' for r in PEOPLE_LOCKS if r not in locks]
        problems += [f"{c['id']}: People's provider wheels must install from PyPI, not ship" for c in people
                     if c.get('lock_role') in PEOPLE_LOCKS and not c.get('wheel_index')]
        for provider in PEOPLE_LOCKS:
            models = [c for c in people if c.get('model_provider') == provider]
            if len(models) != 1 or models[0].get('model_identity') != MODELS[provider]['model']:
                problems.append(f'People needs exactly one {provider} model ({MODELS[provider]["model"]})')
            elif models[0]['sha256'] != model_digest(provider):
                problems.append(f'the {provider} model is not the file setup expects (research-models.json)')
    pinned = {}
    for item in manifest.components.values():
        path = payload_root / str(safe_relative(item['bundle_path']))
        if not path.is_file() or path.stat().st_size != item['byte_size'] or file_sha256(path) != item['sha256']:
            problems.append(f"{item['id']}: payload differs from the manifest")
            continue
        if not item['license_ids'] or not all(item['license_ids']):
            problems.append(f"{item['id']}: no license inventory IDs")
        if zipfile.is_zipfile(path) and item['installed_size_estimate'] < uncompressed(path):
            problems.append(f"{item['id']}: installed_size_estimate is below the real uncompressed size")
        if item.get('lock_role') and item.get('wheel_index'):
            problems += index_lock_problems(item, path)
        elif item.get('lock_role'):
            pinned.update(lock_pins(path))
        if item.get('runtime_role') in ('postgres', 'ffmpeg'):
            info = json.loads(zipfile.ZipFile(path).read('payload-info.json'))
            for formula, entry in sorted(info.get('formulas', {}).items()):
                if not entry.get('licenses') and not entry.get('license'):
                    problems.append(f"{item['id']}: {formula} has no license file and no SPDX license")
                if not entry.get('source_url'):
                    problems.append(f"{item['id']}: no matching source for {formula} {entry['version']}"
                                    + (f" ({entry['source_mismatch']})" if entry.get('source_mismatch') else ''))
        if item.get('runtime_role') and minimum:
            # Every program must run on the oldest macOS the release promises.
            needed = runtime_minimum(path)
            if needed and os_version_tuple(needed[0]) > os_version_tuple(minimum):
                problems.append(f"{item['id']}: {needed[1]} needs macOS {needed[0]}, newer than minimum_os {minimum}")
    shipped = {c['sha256'] for c in manifest.components.values() if c['bundle_path'].endswith('.whl')}
    problems += [f'locked wheel {pin} is not in the release' for digest, pin in sorted(pinned.items()) if digest not in shipped]
    agreement = payload_root / str(safe_relative(doc['agreement_path']))
    if not agreement.is_file() or file_sha256(agreement) != doc['agreement_sha256']:
        problems.append('the agreement differs from the manifest')
    # The agreement promises the corresponding source of copyleft programs with every release.
    problems += missing_sources(manifest, payload_root)
    if problems:
        raise ValueError('Mac release audit failed:\n- ' + '\n- '.join(problems))


# The line under the setup window's choices, about what the app itself fetches before installing.
NOTICES = {'online': 'FamilyFoto Setup downloads a private Python 3.12 (PSF License) and the installer engine.',
           'offline': 'This offline package includes a private Python 3.12 (PSF License) and the installer engine.'}


def descriptor(manifest, engine_zip, notices, mode):
    """bootstrap.json for the app: the bundled Python runtime doubles as the bootstrap Python."""
    python = manifest.components['runtime-python']
    engine = Path(engine_zip)
    base = RELEASE_BASE + '/' + quote(manifest.document['release_id'], safe='')
    return dict(schema_version=1, mode=mode, release_id=manifest.document['release_id'],
                minimum_os=manifest.document['minimum_os'], manifest_sha256=manifest.digest,
                bootstrap_notices=notices,
                python=dict(url=python['url'], sha256=python['sha256'], bundle_path=python['bundle_path']),
                engine=dict(url=base + '/engine.zip', sha256=file_sha256(engine), bundle_path='bootstrap/engine.zip'))


def bundle(release_dir, engine_zip, destination, audited=True):
    """The offline bundle: every payload, the agreement, the manifest, and the bootstrap engine.

    It audits first; only rehearsals that knowingly use stand-ins pass audited=False.
    """
    release_dir = Path(release_dir)
    raw = (release_dir / 'manifest.json').read_bytes()
    manifest = Manifest(raw, sha256(raw))
    if audited:
        audit(manifest, release_dir / 'payload')
    cache = release_dir / 'cache'
    cache.mkdir(exist_ok=True)
    for item in manifest.components.values():
        target = cache / item['sha256']
        if not target.exists():
            shutil.copyfile(release_dir / 'payload' / item['bundle_path'], target)
    agreement = (release_dir / 'payload' / manifest.document['agreement_path']).read_bytes()
    build_bundle(manifest, destination, cache, agreement, raw)
    (Path(destination) / 'bootstrap').mkdir(exist_ok=True)
    shutil.copyfile(engine_zip, Path(destination) / 'bootstrap/engine.zip')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['assemble', 'audit', 'descriptor', 'bundle'])
    p.add_argument('--recipe', type=Path)
    p.add_argument('--release', type=Path, help='output folder of `assemble`')
    p.add_argument('--engine', type=Path, help='engine ZIP from build_launcher.py engine')
    p.add_argument('--mode', choices=['online', 'offline'], default='online')
    p.add_argument('--notices', help='default: the notice for --mode (NOTICES)')
    p.add_argument('--output', type=Path)
    a = p.parse_args(argv)
    if a.command == 'assemble':
        assemble(a.recipe, a.output)
        return
    raw = (a.release / 'manifest.json').read_bytes()
    manifest = Manifest(raw, sha256(raw))
    if a.command == 'audit':
        audit(manifest, a.release / 'payload')
        print('Mac release audit passed:', manifest.digest)
    elif a.command == 'descriptor':
        a.output.write_text(json.dumps(descriptor(manifest, a.engine, a.notices or NOTICES[a.mode], a.mode), indent=1) + '\n')
    else:
        bundle(a.release, a.engine, a.output)


if __name__ == '__main__':
    main()
