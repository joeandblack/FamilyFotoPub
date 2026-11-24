"""Windows release builder (installer/windows/build_release.py), with small stand-in payloads."""
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from installer.common.manifest import Manifest, sha256
from installer.windows import build_release as wr

ROOT = Path(__file__).resolve().parents[1]


def wheel(folder, name, version, license='MIT'):
    path = folder / f'{name}-{version}-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-{version}.dist-info/METADATA',
                         f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nLicense-Expression: {license}\n')
    return path


def lock(path, wheels):
    lines = ['# test lock'] + [f"{w.name.split('-')[0]}=={w.name.split('-')[1]} --hash=sha256:{hashlib.sha256(w.read_bytes()).hexdigest()}"
                               for w in wheels]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


@pytest.fixture
def recipe(tmp_path, monkeypatch):
    work = tmp_path / 'inputs'
    (work / 'wheelhouse').mkdir(parents=True)
    with zipfile.ZipFile(work / 'FamilyFoto-test.zip', 'w') as archive:
        archive.writestr('FamilyFoto/README.md', 'app')
    names = {'python': 'python.3.12.10.nupkg', 'postgres-installer': 'postgresql-17.11-5-windows-x64.exe',
             'postgres-binaries': 'postgresql-17.11-5-windows-x64-binaries.zip'}
    pins = {}
    for cid, name in names.items():
        if name.endswith('.zip'):
            with zipfile.ZipFile(work / name, 'w') as archive:
                archive.writestr('bin/tool.exe', cid * 10)
        else:
            (work / name).write_bytes(cid.encode() * 10)
        pins[cid] = hashlib.sha256((work / name).read_bytes()).hexdigest()
    monkeypatch.setattr(wr, 'setup_pins', lambda root=None: pins)
    core = [wheel(work / 'wheelhouse', 'fastapi', '1.0'), wheel(work / 'wheelhouse', 'psycopg', '3.2.9', 'LGPL-3.0-only'),
            wheel(work / 'wheelhouse', 'psycopg_binary', '3.2.9', 'LGPL-3.0-only')]
    view = [wheel(work / 'wheelhouse', 'numpy', '2.2.6', 'BSD-3-Clause')]
    lock(work / 'core.txt', core)
    lock(work / 'password.txt', core[:1])
    lock(work / 'view.txt', view)
    sources = work / 'sources'
    sources.mkdir()
    (sources / 'psycopg-3.2.9.tar.gz').write_bytes(b'sdist')
    (sources / 'psycopg_c-3.2.9.tar.gz').write_bytes(b'sdist')  # psycopg-binary's source package
    doc = dict(release_id='v0.1.0-beta.2', source_commit='a' * 40, application='FamilyFoto-test.zip',
               payloads=names, locks={'core': 'core.txt', 'password': 'password.txt', 'people-view': 'view.txt'},
               wheels='wheelhouse', sources='sources',
               licenses={'python': ['PSF-2.0'], 'postgres-installer': ['PostgreSQL'],
                         'postgres-binaries': ['PostgreSQL']})
    path = work / 'recipe.json'
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path, tmp_path / 'release'


def test_assembled_release_passes_audit_and_names_both_setups(recipe):
    path, out = recipe
    manifest = wr.assemble(path, out)
    wr.audit(manifest, out / 'payload')
    doc = manifest.document
    assert doc['setup_url'].endswith('/v0.1.0-beta.2/FamilyFotoSetup.exe')
    assert doc['mac_handover'] == {'release_id': 'v0.1.0-beta.2', 'setup_url':
                                   'https://github.com/joeandblack/FamilyFotoPub/releases/download/v0.1.0-beta.2/FamilyFotoSetup.dmg'}
    assert doc['minimum_os'] == wr.MINIMUM_WINDOWS and 'people' not in doc['features']
    ids = {c['id'] for c in manifest.components.values()}
    assert {'application', *wr.NATIVE, 'lock-core', 'lock-password', 'lock-people-view'} <= ids
    # Releases ship no wheels: setup installs every lock from PyPI.
    assert not any(c['bundle_path'].endswith('.whl') for c in manifest.components.values())
    assert json.loads((out / 'assets.json').read_text())['runtime--python.3.12.10.nupkg'] == 'runtime/python.3.12.10.nupkg'


@pytest.mark.parametrize('problem,expected', [
    ('pin', 'not the file Windows setup pins'),
    ('shipped-ffmpeg', 'ffmpeg: must not be shipped'),
    ('mac', 'mac_handover is missing'),
])
def test_audit_refusals(recipe, monkeypatch, problem, expected):
    path, out = recipe
    work = path.parent
    if problem == 'pin':
        monkeypatch.setattr(wr, 'setup_pins', lambda root=None: {**dict.fromkeys(wr.NATIVE, '0' * 64)})
    if problem == 'shipped-ffmpeg':
        (work / 'ffmpeg.zip').write_bytes(b'gpl build')
        doc = json.loads(path.read_text()); doc['payloads']['ffmpeg'] = 'ffmpeg.zip'; path.write_text(json.dumps(doc))
        monkeypatch.setattr(wr, 'NATIVE', (*wr.NATIVE, 'ffmpeg'))
        pins = wr.setup_pins()
        monkeypatch.setattr(wr, 'setup_pins', lambda root=None: {**pins, 'ffmpeg': hashlib.sha256(b'gpl build').hexdigest()})
    if problem == 'mac':
        doc = json.loads(path.read_text()); doc['mac'] = False; path.write_text(json.dumps(doc))
    manifest = wr.assemble(path, out)
    with pytest.raises(ValueError, match=expected):
        wr.audit(manifest, out / 'payload')


def test_descriptor_and_offline_bundle(recipe, tmp_path):
    path, out = recipe
    manifest = wr.assemble(path, out)
    engine = tmp_path / 'engine.zip'
    with zipfile.ZipFile(engine, 'w') as archive:
        archive.writestr('installer-manifest.json', (out / 'manifest.json').read_bytes())
    desc = wr.descriptor(manifest, engine, 'notices', 'offline')
    assert desc['python']['bundle_path'] == 'runtime/python.3.12.10.nupkg'
    assert desc['engine']['url'].endswith('/v0.1.0-beta.2/engine.zip') and desc['manifest_sha256'] == manifest.digest
    wr.bundle(out, engine, tmp_path / 'offline')
    offline = tmp_path / 'offline'
    assert (offline / 'COMPLETE').read_text().strip() == manifest.digest
    assert (offline / 'bootstrap/engine.zip').is_file() and (offline / 'runtime/python.3.12.10.nupkg').is_file()


def test_real_setup_pins_and_windows_locks_agree():
    pins = wr.setup_pins(ROOT)
    assert set(pins) == set(wr.NATIVE)
    from installer.macos.build_release import lock_pins
    password = lock_pins(ROOT / 'installer/locks/password-windows-x64.txt')
    core = lock_pins(ROOT / 'installer/locks/core-windows-x64.txt')
    assert password and set(password) <= set(core)
    # setup-password.ps1 pins the same files and downloads them from PyPI even with a receipt (-Network).
    script = (ROOT / 'installer/windows/setup-password.ps1').read_text(encoding='utf-8')
    # The script installs cffi and cryptography with --no-deps; the lock also pins cffi's pycparser
    # so install_dependencies --role password is a complete closure.
    assert all(digest in script.lower() for digest, pin in password.items() if not pin.startswith('pycparser=='))
    assert any(pin.startswith('pycparser==') for pin in password.values())
    assert '$wheel.Sha256 -Network' in script
    view = lock_pins(ROOT / 'installer/locks/people-view-windows-x64.txt')
    assert set(view) == set(lock_pins(ROOT / 'requirements-faces-windows.txt'))


def test_people_release_ships_models_but_not_provider_libraries(recipe, monkeypatch):
    path, out = recipe
    work = path.parent
    (work / 'models').mkdir()
    (work / 'models/buffalo_l.zip').write_bytes(b'insightface model')
    (work / 'models/recognizer.pt').write_bytes(b'facenet model')
    digests = {'insightface': hashlib.sha256(b'insightface model').hexdigest(),
               'facenet': hashlib.sha256(b'facenet model').hexdigest()}
    monkeypatch.setattr(wr, 'model_digest', lambda provider: digests[provider])
    doc = json.loads(path.read_text())
    doc['models'] = {'insightface': 'models/buffalo_l.zip', 'facenet': 'models/recognizer.pt'}
    doc['licenses'].update({'model-insightface': ['research only'], 'model-facenet': ['research only']})
    path.write_text(json.dumps(doc))
    manifest = wr.assemble(path, out)
    wr.audit(manifest, out / 'payload')
    people = {c['id'] for c in manifest.plan(['core', 'people'], 'windows-x64', '10.0.26300')}
    assert {'model-insightface', 'model-facenet'} <= people
    assert not any(c.get('lock_role') in ('insightface', 'facenet') for c in manifest.components.values())


def test_recipe_with_provider_locks_is_refused(recipe):
    path, out = recipe
    doc = json.loads(path.read_text())
    doc['locks']['insightface'] = doc['locks']['core']
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='installs them from PyPI'):
        wr.assemble(path, out)
