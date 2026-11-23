"""Mac release builder, audit and relocatable native payloads.

The relocation tests use this Mac's Homebrew PostgreSQL/FFmpeg when present and run the relocated
programs from a temporary folder (initdb, a temporary server, an H.264 encode). Nothing else is
installed or started, and Homebrew itself is never changed.
"""
import io
import json
import struct
import os
import platform
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from installer.common.acquire import extract_zip
from installer.common.manifest import Manifest, sha256
from installer.macos import build_payloads, build_release
from installer.macos.bundled import EXECUTABLES
from installer.macos.shared_adapter import verify_tree

ROOT = Path(__file__).resolve().parents[1]
MAC = sys.platform == 'darwin' and platform.machine() == 'arm64'
HOMEBREW = Path('/opt/homebrew')


def has_formula(name):
    return MAC and (HOMEBREW / 'opt' / name).exists() and (HOMEBREW / 'bin/brew').exists()


# --- Relocation -------------------------------------------------------------------------------

def test_resolve_follows_loader_and_rpath_references(tmp_path):
    lib = tmp_path / 'keg/lib'
    lib.mkdir(parents=True)
    (lib / 'libfoo.1.dylib').write_bytes(b'x')
    original = tmp_path / 'keg/bin/tool'
    original.parent.mkdir()
    assert build_payloads.resolve('@loader_path/../lib/libfoo.1.dylib', original, []) == (lib / 'libfoo.1.dylib').resolve()
    assert build_payloads.resolve('@rpath/libfoo.1.dylib', original, ['@loader_path/../lib']) == (lib / 'libfoo.1.dylib').resolve()
    assert build_payloads.resolve(str(lib / 'libfoo.1.dylib'), original, []) == (lib / 'libfoo.1.dylib').resolve()
    with pytest.raises(ValueError, match='Unresolved'):
        build_payloads.resolve('@rpath/libmissing.dylib', original, ['@loader_path/../lib'])
    with pytest.raises(ValueError, match='not relocatable'):
        build_payloads.resolve('@executable_path/../lib/libfoo.1.dylib', original, [])


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def dyld_loads(executable):
    out = subprocess.run([str(executable), '--version'], env={**os.environ, 'DYLD_PRINT_LIBRARIES': '1'},
                         capture_output=True, text=True).stderr
    return [line.split('> ', 1)[-1] for line in out.splitlines() if line.startswith('dyld[') and '> ' in line]


@pytest.mark.skipif(not has_formula('ffmpeg'), reason="needs Apple Silicon with Homebrew's ffmpeg")
def test_relocated_ffmpeg_runs_from_the_payload_only(tmp_path):
    payload = tmp_path / 'ffmpeg.zip'
    build_payloads.build_ffmpeg(payload)
    out = tmp_path / 'ffmpeg'
    extract_zip(payload, out, 1 << 30)
    verify_tree(payload, out)
    loads = dyld_loads(out / 'bin/ffmpeg')
    assert loads and not [p for p in loads if p.startswith(('/opt/homebrew', '/usr/local'))]
    assert any(p.startswith(str(out.resolve())) or p.startswith(str(out)) for p in loads)
    video = tmp_path / 'test.mp4'
    subprocess.run([out / 'bin/ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'testsrc=duration=1:size=160x120:rate=5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', video], check=True)
    probe = subprocess.run([out / 'bin/ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name',
                            '-of', 'csv=p=0', video], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == 'h264'
    info = json.loads((out / 'payload-info.json').read_text())
    assert info['gpl'] and 'ffmpeg' in info['formulas'] and info['formulas']['ffmpeg']['licenses']


@pytest.mark.skipif(not has_formula('postgresql@17'), reason="needs Apple Silicon with Homebrew's postgresql@17")
@pytest.mark.ci_database
def test_relocated_postgres_initializes_and_serves_from_the_payload_only(tmp_path):
    payload = tmp_path / 'postgres.zip'
    build_payloads.build_postgres(payload)
    out = tmp_path / 'postgres'
    extract_zip(payload, out, 1 << 30)
    verify_tree(payload, out)
    real = next(out.glob('Cellar/postgresql@17/*/bin'))
    for tool in ('postgres', 'initdb', 'pg_ctl', 'psql', 'pg_dump'):
        assert not [p for p in dyld_loads(real / tool) if p.startswith(('/opt/homebrew', '/usr/local'))], tool
    bin_ = out / 'bin'
    data, port, env = tmp_path / 'data', free_port(), {**os.environ, 'LC_ALL': 'C'}
    run = subprocess.run([bin_ / 'initdb', '-D', data, '-U', 'payload', '--auth=trust', '-E', 'UTF8', '--locale=C', '-d'],
                         capture_output=True, text=True, env=env, check=True)
    debug = run.stdout + run.stderr  # initdb -d writes its settings to stderr
    share = next(line for line in debug.splitlines() if line.startswith('share_path='))
    assert str(out.resolve()) in str(Path(share.split('=', 1)[1]).resolve())  # not Homebrew's share
    options = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
    subprocess.run([bin_ / 'pg_ctl', '-D', data, '-l', tmp_path / 'pg.log', '-o', options, '-w', 'start'], env=env, check=True,
                   capture_output=True)
    try:
        result = subprocess.run([bin_ / 'psql', '-h', '127.0.0.1', '-p', str(port), '-U', 'payload', '-d', 'postgres', '-Atc',
                                 "create table t(x int); insert into t values (42); select sum(x) from t;"],
                                capture_output=True, text=True, check=True)
        assert result.stdout.strip().endswith('42')
        dump = subprocess.run([bin_ / 'pg_dump', '-h', '127.0.0.1', '-p', str(port), '-U', 'payload', '-d', 'postgres', '-t', 't'],
                              capture_output=True, text=True, check=True).stdout
        assert 'CREATE TABLE public.t' in dump
    finally:
        subprocess.run([bin_ / 'pg_ctl', '-D', data, '-m', 'fast', '-w', 'stop'], env=env, capture_output=True)
    assert '/opt/homebrew' not in (tmp_path / 'pg.log').read_text()


# --- Release assembly and audit ----------------------------------------------------------------

def wheel(path, name, version, license_expression='MIT'):
    with zipfile.ZipFile(path, 'w') as archive:
        meta = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n'
        if license_expression:
            meta += f'License-Expression: {license_expression}\n'
        archive.writestr(f'{name}-{version}.dist-info/METADATA', meta)
    return path


def runtime_zip(path, files, info=None):
    with zipfile.ZipFile(path, 'w') as archive:
        for name in files:
            archive.writestr(name, '#!/bin/sh\n')
        if info is not None:
            archive.writestr('payload-info.json', json.dumps(info))
    return path


def recipe(tmp_path, monkeypatch, *, source=True, wheel_license='MIT', sources=True):
    """A small but complete Mac release recipe made of fixture files."""
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'buffalo_l.zip').write_bytes(b'insightface model fixture')
    (models / 'recognizer.pt').write_bytes(b'facenet model fixture')
    digests = {'insightface': sha256(b'insightface model fixture'), 'facenet': sha256(b'facenet model fixture')}
    monkeypatch.setattr(build_release, 'model_digest', lambda provider: digests[provider])
    wheels = tmp_path / 'wheelhouse'
    wheels.mkdir()
    locks = {}
    # Create shared bytes once: recreating a ZIP can change its timestamp/hash
    # between role locks even when its package contents are identical.
    shared = wheel(wheels / 'shared_pkg-2.0-py3-none-any.whl', 'shared_pkg', '2.0')
    for role in ('core', 'password', 'people-view', 'insightface', 'facenet'):
        w = wheel(wheels / f'{role.replace("-", "_")}_pkg-1.0-py3-none-any.whl', role.replace('-', '_') + '_pkg', '1.0',
                  wheel_license if role == 'insightface' else 'MIT')
        locks[role] = tmp_path / f'{role}.txt'
        locks[role].write_text(f'{role}-pkg==1.0 --hash=sha256:{build_release.file_sha256(w)}\n'
                               f'shared-pkg==2.0 --hash=sha256:{build_release.file_sha256(shared)}\n')
    info = {'formulas': {'demo': {'version': '1.0', 'license': 'MIT',
                                  'source_url': 'https://example.com/demo-1.0.tar.gz' if source else None,
                                  **({} if source else {'source_mismatch': 'installed 1.0, formula 1.1'})}}}
    runtimes = {role: runtime_zip(tmp_path / f'{role}.zip', names, info if role in ('postgres', 'ffmpeg') else None)
                for role, names in EXECUTABLES.items()}
    (tmp_path / 'application.zip').write_bytes(runtime_zip(io.BytesIO(), ['FamilyFoto/README.md']).getvalue())
    (tmp_path / 'agreement.txt').write_text('Fixture terms\n')
    doc = {'release_id': 'test-1', 'source_commit': 'a' * 40, 'minimum_os': '27.0', 'agreement': 'agreement.txt',
           'application': 'application.zip', 'runtimes': {r: str(p) for r, p in runtimes.items()},
           'locks': {r: str(p) for r, p in locks.items()}, 'wheels': 'wheelhouse',
           'models': {'insightface': 'models/buffalo_l.zip', 'facenet': 'models/recognizer.pt'},
           'licenses': {'python': ['PSF-2.0'], 'postgres': ['PostgreSQL'], 'ffmpeg': ['GPL-3.0-or-later'],
                        'setup-window': ['FamilyFoto'], 'model-insightface': ['research'], 'model-facenet': ['research']}}
    if sources:  # the published source of every Homebrew formula (archive and recipe)
        (tmp_path / 'sources/homebrew').mkdir(parents=True)
        (tmp_path / 'sources/demo-1.0.tar.gz').write_bytes(b'demo source')
        (tmp_path / 'sources/homebrew/demo.rb').write_text('class Demo < Formula\nend\n')
        doc['sources'] = 'sources'
    path = tmp_path / 'recipe.json'
    path.write_text(json.dumps(doc))
    return path


def assembled(tmp_path, monkeypatch, **kwargs):
    manifest = build_release.assemble(recipe(tmp_path, monkeypatch, **kwargs), tmp_path / 'release')
    return manifest, tmp_path / 'release'


def test_assembled_release_passes_the_audit(tmp_path, monkeypatch):
    manifest, release = assembled(tmp_path, monkeypatch)
    build_release.audit(manifest, release / 'payload')
    doc = manifest.document
    assert doc['platform'] == 'macos-arm64' and doc['minimum_os'] == '27.0'
    assert doc['setup_url'].endswith('/test-1/FamilyFotoSetup.dmg')
    roles = {c.get('runtime_role') for c in doc['components']}
    assert set(EXECUTABLES) <= roles
    wheels = [c for c in doc['components'] if c['bundle_path'].endswith('.whl')]
    assert len(wheels) == 6  # five role packages plus one shared wheel, placed once
    assets = json.loads((release / 'assets.json').read_text())
    assert assets['runtime--python.zip'] == 'runtime/python.zip'
    assert all(c['url'].endswith('/' + c['bundle_path'].replace('/', '--')) for c in doc['components'])
    people = {c['id'] for c in manifest.plan(['core', 'people'], 'macos-arm64', '27.0')}
    assert {'model-insightface', 'model-facenet', 'lock-insightface', 'lock-facenet'} <= people


def test_release_uses_the_repository_terms_unless_the_recipe_names_others(tmp_path, monkeypatch):
    path = recipe(tmp_path, monkeypatch)
    doc = json.loads(path.read_text())
    del doc['agreement']
    path.write_text(json.dumps(doc))
    manifest = build_release.assemble(path, tmp_path / 'release')
    shipped = tmp_path / 'release/payload' / manifest.document['agreement_path']
    assert shipped.read_bytes() == (ROOT / 'installer/legal/agreement.txt').read_bytes()
    assert manifest.document['agreement_sha256'] == build_release.file_sha256(shipped)
    build_release.audit(manifest, tmp_path / 'release/payload')


def test_release_without_its_sources_fails_the_audit(tmp_path, monkeypatch):
    manifest, release = assembled(tmp_path, monkeypatch, sources=False)
    with pytest.raises(ValueError) as refused:
        build_release.audit(manifest, release / 'payload')
    assert 'sources/ lacks demo-1.0.tar.gz' in str(refused.value)
    assert 'sources/homebrew/ lacks demo.rb' in str(refused.value)


def test_copyleft_wheel_needs_its_source_distribution(tmp_path, monkeypatch):
    manifest, release = assembled(tmp_path, monkeypatch, wheel_license='GNU Lesser General Public License v3 (LGPLv3)')
    with pytest.raises(ValueError, match='lacks the source distribution of insightface_pkg 1.0'):
        build_release.audit(manifest, release / 'payload')
    (release / 'sources/insightface-pkg-1.0.tar.gz').write_bytes(b'sdist')  # any spelling of the name
    build_release.audit(manifest, release / 'payload')


def test_terms_cover_what_a_release_bundles():
    text = (ROOT / 'installer/legal/agreement.txt').read_text(encoding='utf-8')
    assert text.startswith('FamilyFoto Terms and Third-Party Notices\n')
    for required in ('GPL-3.0', 'WITHOUT ANY WARRANTY', 'Python Software Foundation', 'PostgreSQL License',
                     'FFmpeg', 'x264', 'LGPL', 'corresponding source', 'buffalo_l', 'VGGFace2',
                     'NON-COMMERCIAL RESEARCH USE ONLY', 'https://github.com/joeandblack/FamilyFotoPub/releases'):
        assert required in text, required
    assert all(ord(c) < 128 for c in text)  # plain ASCII: shows the same in every setup window


def test_missing_locked_wheel_is_refused_at_assembly(tmp_path, monkeypatch):
    path = recipe(tmp_path, monkeypatch)
    next((tmp_path / 'wheelhouse').glob('facenet*.whl')).unlink()
    with pytest.raises(ValueError, match='wheelhouse lacks facenet-pkg'):
        build_release.assemble(path, tmp_path / 'release')


def rewrite(release, change):
    raw = (release / 'manifest.json').read_bytes()
    doc = json.loads(raw)
    change(doc)
    raw = json.dumps(doc).encode()
    return Manifest(raw, sha256(raw))


@pytest.mark.parametrize('damage, message', [
    ('tamper', 'payload differs'),
    ('size', 'below the real uncompressed size'),
    ('minimum', 'minimum_os must be 27.0'),
    ('model', 'not the file setup expects'),
    ('license', 'no license inventory IDs'),
    ('drop-wheel', 'is not in the release'),
])
def test_audit_refusals(tmp_path, monkeypatch, damage, message):
    manifest, release = assembled(tmp_path, monkeypatch)
    payload = release / 'payload'
    if damage == 'tamper':
        (payload / 'runtime/ffmpeg.zip').write_bytes(b'tampered')
    elif damage == 'size':
        manifest = rewrite(release, lambda d: next(c for c in d['components'] if c['id'] == 'runtime-python').update(installed_size_estimate=1))
    elif damage == 'minimum':
        manifest = rewrite(release, lambda d: d.update(minimum_os='26.0'))
    elif damage == 'model':
        monkeypatch.setattr(build_release, 'model_digest', lambda provider: '0' * 64)
    elif damage == 'license':
        manifest = rewrite(release, lambda d: next(c for c in d['components'] if c['id'] == 'runtime-ffmpeg').update(license_ids=[]))
    else:
        manifest = rewrite(release, lambda d: (d['components'].remove(c := next(c for c in d['components'] if c['id'].startswith('wheel-shared'))),
                                               [f.remove(c['id']) for f in d['features'].values() if c['id'] in f]))
    with pytest.raises(ValueError, match=message):
        build_release.audit(manifest, payload)


def test_native_payload_without_matching_source_is_refused(tmp_path, monkeypatch):
    manifest, release = assembled(tmp_path, monkeypatch, source=False)
    with pytest.raises(ValueError, match=r'no matching source for demo 1.0 \(installed 1.0, formula 1.1\)'):
        build_release.audit(manifest, release / 'payload')


def test_wheel_license_falls_back_to_the_recipe(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='no license inventory IDs'):
        manifest, release = assembled(tmp_path, monkeypatch, wheel_license=None)
        build_release.audit(manifest, release / 'payload')
    path = tmp_path / 'again'
    path.mkdir()
    r = recipe(path, monkeypatch, wheel_license=None)
    doc = json.loads(r.read_text())
    doc['licenses']['wheel:insightface-pkg'] = ['MIT']
    r.write_text(json.dumps(doc))
    manifest = build_release.assemble(r, path / 'release')
    build_release.audit(manifest, path / 'release/payload')


def test_descriptor_and_offline_bundle(tmp_path, monkeypatch):
    manifest, release = assembled(tmp_path, monkeypatch)
    engine = tmp_path / 'engine.zip'
    engine.write_bytes(b'engine fixture')
    d = build_release.descriptor(manifest, engine, 'notices', 'offline')
    assert d['manifest_sha256'] == manifest.digest and d['minimum_os'] == '27.0'
    assert d['python']['bundle_path'] == 'runtime/python.zip'  # the bundled runtime is the bootstrap Python
    assert d['engine']['bundle_path'] == 'bootstrap/engine.zip' and d['engine']['url'].endswith('/test-1/engine.zip')
    build_release.bundle(release, engine, tmp_path / 'bundle')
    assert (tmp_path / 'bundle/COMPLETE').read_text().strip() == manifest.digest
    assert (tmp_path / 'bundle/bootstrap/engine.zip').read_bytes() == b'engine fixture'
    assert (tmp_path / 'bundle/runtime/python.zip').is_file()
    (release / 'payload/runtime/ffmpeg.zip').write_bytes(b'tampered')
    with pytest.raises(ValueError, match='audit failed'):
        build_release.bundle(release, engine, tmp_path / 'bundle2')


@pytest.mark.skipif(not MAC, reason='qualify.sh needs /bin/bash on macOS')
def test_qualification_script_reports_a_non_clean_mac():
    script = ROOT / 'installer/macos/qualify/qualify.sh'
    subprocess.run(['/bin/bash', '-n', script], check=True)
    subprocess.run(['/bin/bash', '-n', ROOT / 'installer/macos/qualify/vm-auto-test.sh'], check=True)
    usage = subprocess.run(['/bin/bash', script], capture_output=True, text=True)
    assert usage.returncode == 2 and 'qualify.sh clean' in usage.stdout
    if MAC:
        result = subprocess.run(['/bin/bash', script, 'clean'], capture_output=True, text=True)
        assert 'Apple Silicon' in result.stdout and result.stdout.strip().endswith('failed')
        if HOMEBREW.exists():
            assert result.returncode == 1 and 'FAIL  Homebrew is present' in result.stdout


# --- Mach-O minimum macOS, read without otool (runs on every OS) --------------------------------

def macho(minos=None, version_min=None, bits=64):
    """A minimal little-endian Mach-O with an LC_BUILD_VERSION and/or LC_VERSION_MIN_MACOSX."""
    pack = lambda v: (int(v.split('.')[0]) << 16) | (int((v.split('.') + ['0'])[1]) << 8)  # noqa: E731
    commands = b''
    if minos:
        commands += struct.pack('<IIIIII', 0x32, 24, 1, pack(minos), pack(minos), 0)
    if version_min:
        commands += struct.pack('<IIII', 0x24, 16, pack(version_min), pack(version_min))
    count = (1 if minos else 0) + (1 if version_min else 0)
    if bits == 64:
        return b'\xcf\xfa\xed\xfe' + struct.pack('<IIIIII', 0x0100000c, 0, 2, count, len(commands), 0) + struct.pack('<I', 0) + commands
    return b'\xce\xfa\xed\xfe' + struct.pack('<IIIIII', 12, 0, 2, count, len(commands), 0) + commands


def fat(*slices):
    header = struct.pack('>II', 0xcafebabe, len(slices))
    offset, table, body = 8 + 20 * len(slices), b'', b''
    for data in slices:
        table += struct.pack('>IIIII', 0x0100000c, 0, offset + len(body), len(data), 0)
        body += data
    return header + table + body


def test_macho_minimum_reads_thin_universal_and_older_headers():
    assert build_payloads.macho_minimum_bytes(macho('27.2')) == '27.2'
    assert build_payloads.macho_minimum_bytes(macho(version_min='10.13', bits=32)) == '10.13'
    assert build_payloads.macho_minimum_bytes(fat(macho('14.0'), macho('27.2'))) == '27.2'  # newest slice wins
    assert build_payloads.macho_minimum_bytes(b'#!/bin/sh\n') is None
    assert build_payloads.macho_minimum_bytes(macho()) is None


@pytest.mark.skipif(not MAC, reason='compares with otool')
def test_macho_minimum_matches_otool_on_a_real_binary():
    out = subprocess.run(['/usr/bin/otool', '-arch', 'all', '-l', '/bin/ls'], capture_output=True, text=True).stdout
    expected = max((line.split()[1] for line in out.splitlines() if line.strip().startswith('minos ')),
                   key=lambda v: tuple(map(int, v.split('.'))))
    assert build_payloads.macho_minimum('/bin/ls') in (expected, expected.removesuffix('.0'))


def test_runtime_minimum_never_writes_archive_names(tmp_path):
    archive = tmp_path / 'evil.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('../outside/bin/tool', macho('28.0'))
        z.writestr('/abs/lib/libx.dylib', macho('27.1'))
        z.writestr('bin/script', '#!/bin/sh\n')
    assert build_release.runtime_minimum(archive) == ('28.0', '../outside/bin/tool')
    assert not (tmp_path.parent / 'outside').exists() and not Path('/abs').exists()


def test_audit_refuses_a_program_newer_than_minimum_os_and_unlicensed_formulas(tmp_path, monkeypatch):
    manifest, release = assembled(tmp_path, monkeypatch)
    payload = release / 'payload'
    with zipfile.ZipFile(payload / 'runtime/ffmpeg.zip', 'a') as z:
        z.writestr('lib/libnew.dylib', macho('28.0'))
    info = {'formulas': {'quiet': {'version': '1.0', 'licenses': [], 'license': None, 'source_url': 'https://example.com/q.tgz'}}}
    with zipfile.ZipFile(payload / 'runtime/postgres.zip', 'w') as z:
        z.writestr('bin/postgres', '#!/bin/sh\n')
        z.writestr('payload-info.json', json.dumps(info))
    def resize(doc):
        for c in doc['components']:
            if c['id'] in ('runtime-ffmpeg', 'runtime-postgres'):
                path = payload / c['bundle_path']
                c.update(byte_size=path.stat().st_size, sha256=build_release.file_sha256(path),
                         installed_size_estimate=build_release.uncompressed(path))
    manifest = rewrite(release, resize)
    with pytest.raises(ValueError) as error:
        build_release.audit(manifest, payload)
    assert 'runtime-ffmpeg: lib/libnew.dylib needs macOS 28.0, newer than minimum_os 27.0' in str(error.value)
    assert 'runtime-postgres: quiet has no license file and no SPDX license' in str(error.value)
