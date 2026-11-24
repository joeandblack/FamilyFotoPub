"""Release-side: assemble and audit a Windows release of the shared installer.

  assemble   recipe JSON -> payload tree (bundle_path layout), manifest.json, assets.json
  audit      refuse a release that is incomplete, mis-sized, unpinned or missing sources
  descriptor bootstrap.json for FamilyFotoSetup.exe (online or offline)
  bundle     offline payload bundle (installer/common/bundle.py) plus the bootstrap engine

The recipe names local, already-downloaded inputs; nothing here downloads or publishes:

  {"release_id": "v0.1.0-beta.2", "source_commit": "<40 hex>",
   "minimum_os": "10.0.22000",              (optional; default MINIMUM_WINDOWS)
   "agreement": "agreement.txt",            (optional; default installer/legal/agreement.txt)
   "application": "FamilyFoto-0.1.0b2.zip", (installer/release.py build, from the tagged commit)
   "payloads": {"python": "python.3.12.10.nupkg",
                "postgres-installer": "postgresql-17.11-5-windows-x64.exe",
                "postgres-binaries": "postgresql-17.11-5-windows-x64-binaries.zip",
                "ffmpeg": "ffmpeg-9.0.2-essentials_build.zip"},
   "locks": {"core": "installer/locks/core-windows-x64.txt", "password": ..., "people-view": ...},
   "wheels": "wheelhouse/",                 (every wheel the locks pin)
   "licenses": {"python": ["PSF-2.0"], "ffmpeg": ["GPL-3.0-or-later"], "wheel:psycopg": [...], ...},
   "sources": "sources/",                   (published with the release; `audit` lists what it must hold)
   "mac": true}                             (optional, default true: the Mac setup is published under
                                             the same release_id, so new backups name it)

People (the insightface/facenet locks and models) is added only when the recipe names them;
without them the release offers Core, which still restores People catalogs for viewing.

Each native payload must be byte-for-byte the file Windows setup pins (setup-common.ps1,
install-python.ps1, install-ffmpeg.ps1): setup finds prepared payloads by that SHA-256 and never
falls back to the network. Assets are published flat: `bundle_path` with `/` replaced by `--`.
"""
import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer.common.bundle import build as build_bundle  # noqa: E402
from installer.common.manifest import RELEASE_BASE, SETUP_ASSETS, Manifest, safe_relative, sha256  # noqa: E402
from installer.macos.build_release import (COPYLEFT, MODELS, asset_name, file_sha256, lock_pins,  # noqa: E402
                                           model_digest, normalized, uncompressed, wheel_license)
from installer.windows.build_shared import audit_windows_release  # noqa: E402

PLATFORM = 'windows-x64'
MINIMUM_WINDOWS = '10.0.22000'   # Windows 11 (install.ps1 refuses older builds)
NATIVE = ('python', 'postgres-installer', 'postgres-binaries', 'ffmpeg')
CORE_LOCKS = ('core', 'password', 'people-view')
PEOPLE_LOCKS = ('insightface', 'facenet')
AGREEMENT = ROOT / 'installer/legal/agreement.txt'
# A binary-only wheel whose source is published as another PyPI package of the same version.
SOURCE_PACKAGES = {'psycopg-binary': 'psycopg-c'}


def setup_pins(root=ROOT):
    """{payload id: SHA-256} as the Windows setup scripts pin them, read from those scripts."""
    windows = Path(root) / 'installer/windows'
    common = (windows / 'setup-common.ps1').read_text(encoding='utf-8')
    python = (windows / 'install-python.ps1').read_text(encoding='utf-8')
    ffmpeg = (windows / 'install-ffmpeg.ps1').read_text(encoding='utf-8')
    found = {
        'python': re.search(r"\$PythonNuGetSha256 = '([0-9A-Fa-f]{64})'", python),
        'postgres-installer': re.search(r"\$PostgresInstallerSha256 = '([0-9A-Fa-f]{64})'", common),
        'postgres-binaries': re.search(r"\$PostgresBinariesSha256 = '([0-9A-Fa-f]{64})'", common),
        'ffmpeg': re.search(r"\$expected='([0-9A-Fa-f]{64})'", ffmpeg),
    }
    missing = [key for key, match in found.items() if not match]
    if missing:
        raise ValueError('Cannot read the Windows setup pin for: ' + ', '.join(missing))
    return {key: match.group(1).lower() for key, match in found.items()}


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
    people = all(role in recipe.get('locks', {}) for role in PEOPLE_LOCKS) and 'models' in recipe
    components, features = [], {'core': [], **({'people': []} if people else {})}
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
    for cid in NATIVE:
        source = path(recipe['payloads'][cid])
        # Keep the upstream file name: it is what each payload is, for anyone reading the release.
        add(cid, source, f'runtime/{source.name}', 'core', licenses.get(cid, []))
    wheelhouse = {file_sha256(w): w for w in sorted(path(recipe['wheels']).glob('*.whl'))}
    placed = set()
    for role in (*CORE_LOCKS, *(PEOPLE_LOCKS if people else ())):
        feature = 'core' if role in CORE_LOCKS else 'people'
        lock = path(recipe['locks'][role])
        add('lock-' + role, lock, f'locks/{role}.txt', feature, ['FamilyFoto'], lock_role=role)
        for digest, pin in sorted(lock_pins(lock).items()):
            if digest in placed:
                continue
            if digest not in wheelhouse:
                raise ValueError(f'The wheelhouse lacks {pin} ({role} lock)')
            wheel = wheelhouse[digest]
            name = normalized(wheel.name.split('-')[0])
            add('wheel-' + re.sub('[^a-z0-9]+', '-', wheel.name.lower())[:100].strip('-'), wheel,
                'wheels/' + wheel.name, feature, licenses.get('wheel:' + name) or wheel_license(wheel))
            placed.add(digest)
    if people:
        for provider in PEOPLE_LOCKS:
            source = path(recipe['models'][provider])
            add('model-' + provider, source, f'models/{provider}/{source.name}', 'people',
                licenses.get('model-' + provider, []), model_provider=provider,
                model_identity=MODELS[provider]['model'])
    agreement = path(recipe['agreement']) if 'agreement' in recipe else AGREEMENT
    if 'sources' in recipe:
        shutil.copytree(path(recipe['sources']), output / 'sources')
    (payload / 'legal').mkdir()
    shutil.copyfile(agreement, payload / 'legal' / agreement.name)
    doc = dict(schema_version=1, installer_version=1, release_id=recipe['release_id'],
               source_commit=recipe['source_commit'], platform=PLATFORM,
               minimum_os=recipe.get('minimum_os', MINIMUM_WINDOWS),
               setup_url=release_url + '/' + SETUP_ASSETS[PLATFORM],
               agreement_path='legal/' + agreement.name, agreement_sha256=file_sha256(agreement),
               components=components, features=features)
    if recipe.get('mac', True):
        # Both platforms ship under one release_id, so a Windows backup names the matching Mac setup.
        doc['mac_handover'] = dict(release_id=recipe['release_id'],
                                   setup_url=release_url + '/' + SETUP_ASSETS['macos-arm64'])
    raw = json.dumps(doc, indent=1, sort_keys=True).encode()
    (output / 'manifest.json').write_bytes(raw)
    (output / 'assets.json').write_text(json.dumps(
        {asset_name(c['bundle_path']): c['bundle_path'] for c in components}, indent=1, sort_keys=True))
    return Manifest(raw, sha256(raw))  # run `audit` before publishing; `bundle` refuses without it


def missing_sources(manifest, payload_root):
    """What the release must publish as source but sources/ (beside payload/) lacks.

    FFmpeg (GPL) needs its release source archive under sources/ffmpeg/, plus SOURCES.md there,
    the hand-checked inventory of the libraries the build enables and where their sources are;
    every wheel under a copyleft licence needs its source distribution under sources/. This is a
    floor, not a completeness proof: release review checks the inventory by hand (#20, step 3)."""
    sources = Path(payload_root).parent / 'sources'
    present = {normalized(p.name): p for p in sources.rglob('*') if p.is_file()} if sources.is_dir() else {}
    problems = []
    ffmpeg = manifest.components.get('ffmpeg')
    if ffmpeg:
        version = re.search(r'ffmpeg-([0-9.]+)-', Path(ffmpeg['bundle_path']).name)
        stem = normalized(f'ffmpeg-{version.group(1)}') if version else None
        if not stem or not any(present.get(stem + suffix) for suffix in ('-tar-xz', '-tar-gz', '-tar-bz2')):
            problems.append('ffmpeg: sources/ffmpeg/ lacks the FFmpeg release source archive '
                            + (f'ffmpeg-{version.group(1)}.tar.xz' if version else ''))
        inventory = sources / 'ffmpeg' / 'SOURCES.md'
        if not inventory.is_file():
            problems.append('ffmpeg: sources/ffmpeg/SOURCES.md, the inventory of enabled libraries and their sources, is missing')
        elif not re.search(r'^Status: complete\b', inventory.read_text(encoding='utf-8'), re.M):
            # Written by the release reviewer after checking every library's source by hand.
            problems.append('ffmpeg: sources/ffmpeg/SOURCES.md does not say "Status: complete"; '
                            'the bundled libraries\' sources are not confirmed')
    for item in manifest.components.values():
        if item['bundle_path'].endswith('.whl') and any(COPYLEFT.search(i) for i in item['license_ids']):
            name, version = Path(item['bundle_path']).name.split('-')[:2]
            name = SOURCE_PACKAGES.get(normalized(name), name)
            stem = normalized(f'{name}-{version}')
            archive = present.get(stem + '-tar-gz') or present.get(stem + '-zip')
            if not archive:
                problems.append(f"{item['id']}: sources/ lacks the source distribution of {name} {version} "
                                f"({', '.join(item['license_ids'])})")
    return problems


def audit(manifest, payload_root):
    """Refuse an incomplete or inconsistent Windows release before anything is published."""
    doc, payload_root = manifest.document, Path(payload_root)
    problems = []
    try:
        audit_windows_release(manifest, payload_root)   # required payloads, locks, bytes, agreement
    except (ValueError, OSError) as error:
        problems.append(str(error))
    pins = setup_pins()
    for cid in NATIVE:
        item = manifest.components.get(cid)
        if item and item['sha256'] != pins[cid]:
            problems.append(f'{cid}: not the file Windows setup pins ({pins[cid]}); setup could not find it offline')
    if 'mac_handover' not in doc:
        problems.append('mac_handover is missing: new Windows backups would not name the Mac setup')
    pinned = {}
    for item in manifest.components.values():
        path = payload_root / str(safe_relative(item['bundle_path']))
        if not path.is_file():
            continue  # already reported by the shared audit
        if zipfile.is_zipfile(path) and item['installed_size_estimate'] < uncompressed(path):
            problems.append(f"{item['id']}: installed_size_estimate is below the real uncompressed size")
        if item.get('lock_role'):
            pinned.update(lock_pins(path))
    shipped = {c['sha256'] for c in manifest.components.values() if c['bundle_path'].endswith('.whl')}
    problems += [f'locked wheel {pin} is not in the release' for digest, pin in sorted(pinned.items()) if digest not in shipped]
    if 'people' in doc['features']:
        for item in manifest.components.values():
            if item.get('model_provider') and item['sha256'] != model_digest(item['model_provider']):
                problems.append(f"the {item['model_provider']} model is not the file setup expects (research-models.json)")
    problems += missing_sources(manifest, payload_root)
    if problems:
        raise ValueError('Windows release audit failed:\n- ' + '\n- '.join(problems))


def descriptor(manifest, engine_zip, notices, mode):
    """bootstrap.json for FamilyFotoSetup.exe: the pinned Python NuGet package bootstraps setup."""
    python = manifest.components['python']
    base = RELEASE_BASE + '/' + quote(manifest.document['release_id'], safe='')
    return dict(schema_version=1, mode=mode, release_id=manifest.document['release_id'],
                minimum_os=manifest.document['minimum_os'], manifest_sha256=manifest.digest,
                bootstrap_notices=notices,
                python=dict(url=python['url'], sha256=python['sha256'], bundle_path=python['bundle_path']),
                engine=dict(url=base + '/engine.zip', sha256=file_sha256(engine_zip),
                            bundle_path='bootstrap/engine.zip'))


def bundle(release_dir, engine_zip, destination, audited=True):
    """The offline bundle: every payload, the agreement, the manifest and the bootstrap engine."""
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
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('assemble'); a.add_argument('--recipe', type=Path, required=True); a.add_argument('--output', type=Path, required=True)
    u = sub.add_parser('audit'); u.add_argument('--release', type=Path, required=True)
    d = sub.add_parser('descriptor'); d.add_argument('--release', type=Path, required=True)
    d.add_argument('--engine', type=Path, required=True); d.add_argument('--mode', choices=['online', 'offline'], required=True)
    d.add_argument('--notices', required=True); d.add_argument('--output', type=Path, required=True)
    b = sub.add_parser('bundle'); b.add_argument('--release', type=Path, required=True)
    b.add_argument('--engine', type=Path, required=True); b.add_argument('--output', type=Path, required=True)
    args = p.parse_args(argv)
    if args.command == 'assemble':
        manifest = assemble(args.recipe, args.output)
        print(manifest.digest)
        return
    raw = (args.release / 'manifest.json').read_bytes()
    manifest = Manifest(raw, sha256(raw))
    if args.command == 'audit':
        audit(manifest, args.release / 'payload')
        print('audit passed', manifest.digest)
    elif args.command == 'descriptor':
        if args.output.exists():
            p.error('Output already exists')
        args.output.write_text(json.dumps(descriptor(manifest, args.engine, args.notices, args.mode),
                                          indent=1, sort_keys=True) + '\n', encoding='utf-8')
    else:
        bundle(args.release, args.engine, args.output)


if __name__ == '__main__':
    main()
