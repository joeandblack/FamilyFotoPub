import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from installer.common.acquire import Acquisition, extract_zip, file_digest
from installer.common.manifest import Manifest, safe_relative
from installer.common.workflow import prepare
from installer.common.local_payloads import payload, pip_arguments
from installer.common.lock_wheels import lock
from installer.common.materialize import materialize
from installer.windows.shared_adapter import stage_application
from installer.windows.build_shared import audit_windows_release, build_engine, build_exe
from app.migration.progress_lines import parse_line


def digest(data):
    return hashlib.sha256(data).hexdigest()


def component(key='core', data=b'payload', **changes):
    return dict(id=key, bundle_path=f'payload/{key}.zip', byte_size=len(data),
                installed_size_estimate=1024, sha256=digest(data),
                url=f'https://example.com/{key}', dependencies=[], license_ids=['test'], **changes)


def document(items=None):
    return dict(schema_version=1, installer_version=1, release_id='test-1',
                source_commit='a'*40, platform='windows-x64',
                agreement_path='legal/agreement.txt', agreement_sha256=digest(b'terms'),
                components=items or [component()], features={'core': ['core']})


def manifest(doc=None):
    raw = json.dumps(doc or document()).encode()
    return Manifest(raw, digest(raw))


@pytest.mark.parametrize('path', ['../a', '/a', 'a/../b', 'C:/x', 'a\\b', 'a//b',
                                 'a/./b', 'con.txt', 'a/NUL', 'a.', 'a '])
def test_bad_paths(path):
    with pytest.raises(ValueError):
        safe_relative(path)


def test_release_pinning_and_platform():
    raw = json.dumps(document()).encode()
    with pytest.raises(ValueError, match='digest'):
        Manifest(raw, '0'*64)
    with pytest.raises(ValueError, match='platform'):
        manifest().plan(['core'], 'macos-arm64')


def test_mac_manifest_accepts_dmg_link_only():
    doc = document()
    doc['platform'] = 'macos-arm64'
    doc['setup_url'] = 'https://github.com/joeandblack/FamilyFotoPub/releases/download/test-1/FamilyFotoSetup.dmg'
    assert manifest(doc).plan(['core'], 'macos-arm64')
    doc['setup_url'] = doc['setup_url'].replace('.dmg', '.exe')
    with pytest.raises(ValueError):
        manifest(doc)


@pytest.mark.parametrize('change', ['version', 'cycle', 'missing', 'duplicate', 'http', 'size'])
def test_invalid_manifest(change):
    doc = document()
    if change == 'version':
        doc['schema_version'] = 2
    elif change == 'cycle':
        doc['components'][0]['dependencies'] = ['core']
    elif change == 'missing':
        doc['components'][0]['dependencies'] = ['absent']
    elif change == 'duplicate':
        doc['components'].append(doc['components'][0])
    elif change == 'http':
        doc['components'][0]['url'] = 'http://example.com/a'
    else:
        doc['components'][0]['byte_size'] = -1
    with pytest.raises(ValueError):
        manifest(doc)


def test_dependency_order():
    doc = document([component(), component('python')])
    doc['components'][0]['dependencies'] = ['python']
    assert [c['id'] for c in manifest(doc).plan(['core'], 'windows-x64')] == ['python', 'core']


def test_online_verify_and_cache(tmp_path):
    calls = []
    def opener(url):
        calls.append(url)
        return io.BytesIO(b'payload')
    acquire = Acquisition(tmp_path, opener=opener)
    first = acquire.ensure(component())
    assert acquire.ensure(component()) == first
    assert len(calls) == 1
    first.write_bytes(b'changed')
    with pytest.raises(ValueError, match='Corrupt'):
        acquire.ensure(component())
    assert len(calls) == 1


@pytest.mark.parametrize('data', [b'bad', b'too many bytes for this payload'])
def test_bad_download_never_published(tmp_path, data):
    with pytest.raises(ValueError):
        Acquisition(tmp_path, opener=lambda url: io.BytesIO(data)).ensure(component())
    assert not list(tmp_path.iterdir())


def test_offline_never_calls_network(tmp_path):
    def forbidden(url):
        pytest.fail('offline called network')
    with pytest.raises(ValueError, match='Missing offline'):
        Acquisition(tmp_path / 'cache', mode='offline', opener=forbidden).ensure(component())
    bundle = tmp_path / 'bundle'
    (bundle / 'payload').mkdir(parents=True)
    (bundle / 'payload/core.zip').write_bytes(b'payload')
    target = Acquisition(tmp_path / 'cache', mode='offline', bundle=bundle,
                         opener=forbidden).ensure(component())
    assert target.read_bytes() == b'payload'


def test_consent_before_writes(tmp_path):
    target = tmp_path / 'attempt'
    with pytest.raises(ValueError, match='acceptance'):
        prepare(manifest(), platform='windows-x64', features=['core'], agreement=b'terms',
                accepted_digest='', directory=target, mode='online')
    assert not target.exists()


def test_resume_reverifies_payload_and_events_parse(tmp_path):
    events = io.StringIO()
    args = dict(platform='windows-x64', features=['core'], agreement=b'terms',
                accepted_digest=digest(b'terms'), directory=tmp_path, mode='online', stream=events,
                opener=lambda url: io.BytesIO(b'payload'))
    result = prepare(manifest(), **args)
    assert prepare(manifest(), **args) == result
    parsed = [parse_line(line) for line in events.getvalue().splitlines()]
    assert all(event and event['schema_version'] == 1 for event in parsed)
    assert parsed[-1]['state'] == 'completed'
    Path(result['paths']['core']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='Corrupt'):
        prepare(manifest(), **args)


def test_resume_cannot_change_features(tmp_path):
    doc = document()
    doc['features']['people'] = ['core']
    args = dict(platform='windows-x64', agreement=b'terms', accepted_digest=digest(b'terms'),
                directory=tmp_path, mode='online', opener=lambda url: io.BytesIO(b'payload'))
    prepare(manifest(doc), features=['core'], **args)
    with pytest.raises(ValueError, match='Attempt changed'):
        prepare(manifest(doc), features=['core', 'people'], **args)


def test_cooperative_cancel_does_not_publish_partial(tmp_path):
    calls = []
    def cancelled():
        calls.append(1)
        return len(calls) > 1
    with pytest.raises(InterruptedError):
        prepare(manifest(), platform='windows-x64', features=['core'], agreement=b'terms',
                accepted_digest=digest(b'terms'), directory=tmp_path, mode='online',
                opener=lambda url: io.BytesIO(b'payload'), cancelled=cancelled)
    assert not (tmp_path / 'prepared.json').exists()
    assert not list((tmp_path / 'cache').iterdir())


def test_install_location_is_pinned_for_resume(tmp_path):
    args = dict(platform='windows-x64', features=['core'], agreement=b'terms',
                accepted_digest=digest(b'terms'), directory=tmp_path, mode='online',
                opener=lambda url: io.BytesIO(b'payload'))
    prepare(manifest(), install_root=tmp_path / 'first', **args)
    with pytest.raises(ValueError, match='Attempt changed'):
        prepare(manifest(), install_root=tmp_path / 'second', **args)


@pytest.mark.parametrize('kind', ['traversal', 'symlink', 'duplicate', 'budget'])
def test_archive_validation_before_extraction(tmp_path, kind):
    archive = tmp_path / 'archive.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        if kind == 'traversal':
            out.writestr('../escape', b'bad')
        elif kind == 'symlink':
            entry = zipfile.ZipInfo('link')
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            out.writestr(entry, '../escape')
        elif kind == 'duplicate':
            out.writestr('A', 'a')
            out.writestr('a', 'b')
        else:
            out.writestr('data', b'123456')
    target = tmp_path / 'out'
    with pytest.raises(ValueError):
        extract_zip(archive, target, 4)
    assert not target.exists()


def test_valid_archive(tmp_path):
    archive = tmp_path / 'archive.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr('folder/file', b'yes')
    extract_zip(archive, tmp_path / 'out', 10)
    assert (tmp_path / 'out/folder/file').read_bytes() == b'yes'
    with pytest.raises(ValueError):
        extract_zip(archive, tmp_path / 'out', 10)


def test_local_payload_forbids_fallback(tmp_path, monkeypatch):
    receipt = tmp_path / 'prepared.json'
    receipt.write_text(json.dumps({'schema_version': 1, 'stage': 'acquired', 'payloads': {}}))
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', str(receipt))
    with pytest.raises(ValueError, match='fallback'):
        payload('a'*64)


def test_windows_materialization(tmp_path, monkeypatch):
    data = b'package==1 --hash=sha256:' + b'a'*64
    item = component(data=data, lock_role='core')
    item['bundle_path'] = 'locks/core.txt'
    doc = manifest(document([item]))
    prepare(doc, platform='windows-x64', features=['core'], agreement=b'terms',
                      accepted_digest=digest(b'terms'), directory=tmp_path, mode='online',
                      opener=lambda url: io.BytesIO(data), stream=io.StringIO())
    path = tmp_path / 'prepared.json'
    materialize(doc, path, 'windows-x64')
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', str(path))
    flags = pip_arguments('core')
    assert '--no-index' in flags and '--require-hashes' in flags
    assert payload(item['sha256']).read_bytes() == data
    (tmp_path / 'locks/core.txt').write_text('changed')
    with pytest.raises(ValueError, match='modified'):
        pip_arguments('core')


def test_wheel_lock(tmp_path):
    with zipfile.ZipFile(tmp_path / 'demo-1-py3-none-any.whl', 'w') as archive:
        archive.writestr('demo-1.dist-info/METADATA', 'Name: demo\nVersion: 1\n')
    assert lock(tmp_path) == f'demo==1 --hash=sha256:{file_digest(tmp_path / "demo-1-py3-none-any.whl")}\n'


@pytest.mark.parametrize('failure', ['interrupt', 'corrupt'])
def test_materialization_failure_can_retry(tmp_path, monkeypatch, failure):
    import importlib
    module = importlib.import_module('installer.common.materialize')
    item = component(lock_role='core')
    item['bundle_path'] = 'locks/core.txt'
    release = manifest(document([item]))
    prepare(release, platform='windows-x64', features=['core'], agreement=b'terms',
            accepted_digest=digest(b'terms'), directory=tmp_path, mode='online',
            opener=lambda url: io.BytesIO(b'payload'), stream=io.StringIO())
    receipt = tmp_path / 'prepared.json'
    original = receipt.read_bytes()
    def broken_copy(src, dst):
        dst.write(b'partial')
        if failure == 'interrupt':
            raise InterruptedError('simulated interruption')
    with monkeypatch.context() as patch:
        patch.setattr(module.shutil, 'copyfileobj', broken_copy)
        with pytest.raises((InterruptedError, ValueError)):
            materialize(release, receipt, 'windows-x64')
    assert receipt.read_bytes() == original
    assert not list((tmp_path / 'locks').iterdir())
    result = materialize(release, receipt, 'windows-x64')
    assert result['stage'] == 'materialized'
    assert (tmp_path / 'locks/core.txt').read_bytes() == b'payload'


def test_offline_bundle_through_application_staging(tmp_path, monkeypatch):
    from installer.common.bundle import build
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr('FamilyFoto/installer/windows/install.ps1', '# fixture only')
    data = {'application': archive.getvalue(), 'lock': b'demo==1\n',
            'wheel': b'fixture wheel bytes'}
    items = [component(key, value) for key, value in data.items()]
    items[1].update(bundle_path='locks/core.txt', lock_role='core')
    items[2]['bundle_path'] = 'wheels/demo-1-py3-none-any.whl'
    doc = document(items)
    doc['features']['core'] = list(data)
    raw = json.dumps(doc).encode()
    release = Manifest(raw, digest(raw))
    cache = tmp_path / 'release-cache'
    cache.mkdir()
    for item in items:
        (cache / item['sha256']).write_bytes(data[item['id']])
    # Building from verified cache and all subsequent preparation must be offline.
    monkeypatch.setattr('urllib.request.OpenerDirector.open',
                        lambda *a, **kw: pytest.fail('unexpected network request'))
    bundle = tmp_path / 'bundle'
    build(release, bundle, cache, b'terms', raw)
    assert (bundle / 'COMPLETE').read_text().strip() == release.digest
    attempt = tmp_path / 'attempt'
    target = tmp_path / 'installation'
    args = dict(platform='windows-x64', features=['core'], agreement=b'terms',
                accepted_digest=digest(b'terms'), directory=attempt, mode='offline',
                bundle=bundle, stream=io.StringIO(), install_root=target)
    prepare(release, **args)
    receipt = materialize(release, attempt / 'prepared.json', 'windows-x64')
    stage_application(release, receipt, target)
    assert (target / 'installer/windows/install.ps1').read_text() == '# fixture only'
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', str(attempt / 'prepared.json'))
    assert '--no-index' in pip_arguments('core')
    assert (Path(receipt['wheelhouse']) / 'demo-1-py3-none-any.whl').read_bytes() == data['wheel']
    # Repeating preparation uses the same cache, receipt and owned application tree.
    prepare(release, **args)
    receipt = materialize(release, attempt / 'prepared.json', 'windows-x64')
    stage_application(release, receipt, target)


@pytest.mark.parametrize('value', [True, 14, '', '14.beta', '14.0.0.0.1', '-1', None])
def test_invalid_minimum_os(value):
    doc = document()
    doc['minimum_os'] = value
    with pytest.raises(ValueError, match='OS version'):
        manifest(doc)


def test_minimum_os_before_writes_and_download(tmp_path):
    doc = document()
    doc.update(platform='macos-arm64', minimum_os='14.0')
    release = manifest(doc)
    assert release.plan(['core'], 'macos-arm64', '14.0.0')
    assert release.plan(['core'], 'macos-arm64', '15.0')
    with pytest.raises(ValueError, match='requires'):
        prepare(release, platform='macos-arm64', os_version='13.6.9', features=['core'],
                agreement=b'terms', accepted_digest=digest(b'terms'),
                directory=tmp_path / 'attempt', mode='online',
                opener=lambda url: pytest.fail('must not download'))
    assert not (tmp_path / 'attempt').exists()


@pytest.mark.parametrize('role', ['unknown', '', None, 7])
def test_invalid_lock_role(role):
    item = component(lock_role=role)
    item['bundle_path'] = 'locks/core.txt'
    with pytest.raises(ValueError, match='lock role'):
        manifest(document([item]))


def test_duplicate_lock_roles():
    items = [component('core', lock_role='core'), component('second', lock_role='core')]
    for item in items:
        item['bundle_path'] = f"locks/{item['id']}.txt"
    with pytest.raises(ValueError, match='lock role'):
        manifest(document(items))


def test_macos_password_materialization(tmp_path, monkeypatch):
    core, password = component(lock_role='core'), component('password', lock_role='password')
    core['bundle_path'], password['bundle_path'] = 'locks/core.txt', 'locks/password.txt'
    core['dependencies'] = ['password']
    doc = document([core, password])
    doc.update(platform='macos-arm64', minimum_os='14.0')
    release = manifest(doc)
    result = prepare(release, platform='macos-arm64', os_version='14.0', features=['core'],
                     agreement=b'terms', accepted_digest=digest(b'terms'), directory=tmp_path,
                     mode='online', opener=lambda url: io.BytesIO(b'payload'))
    assert result['stage'] == 'acquired'
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', str(tmp_path / 'prepared.json'))
    with pytest.raises(ValueError, match='materialized'):
        pip_arguments('password')
    result = materialize(release, tmp_path / 'prepared.json', 'macos-arm64', '14.0')
    assert result['stage'] == 'materialized'
    assert '--no-index' in pip_arguments('password')


@pytest.mark.skipif(__import__('sys').platform == 'win32', reason='POSIX executable permissions')
def test_runtime_permissions_without_privileged_bits(tmp_path):
    archive = tmp_path / 'runtime.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        for name, mode in [('bin/python3.12', 0o6755), ('lib/data', 0o666)]:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            out.writestr(info, b'content')
    extract_zip(archive, tmp_path / 'runtime', 100)
    assert stat.S_IMODE((tmp_path / 'runtime/bin/python3.12').stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / 'runtime/lib/data').stat().st_mode) == 0o644


@pytest.mark.parametrize('prefix', ['', 'FamilyFoto/'])
def test_application_stage_refuses_existing_and_changed(tmp_path, prefix):
    archive = tmp_path / 'app.zip'
    with zipfile.ZipFile(archive, 'w') as out:
        out.writestr(prefix + 'installer/windows/install.ps1', 'trusted code')
    data = archive.read_bytes()
    item = component('application', data)
    doc = document([item])
    doc['features']['core'] = ['application']
    doc['setup_url'] = 'https://github.com/joeandblack/FamilyFotoPub/releases/download/test-1/FamilyFotoSetup.exe'
    release = manifest(doc)
    receipt = {'paths': {'application': str(archive)}}
    target = tmp_path / 'app'
    stage_application(release, receipt, target)
    marker = json.loads((target / '.shared-installer-release.json').read_text())
    assert marker['windows_handover']['setup_url'] == doc['setup_url']
    stage_application(release, receipt, target)
    (target / 'installer/windows/install.ps1').write_text('changed')
    with pytest.raises(ValueError, match='changed'):
        stage_application(release, receipt, target)
    existing = tmp_path / 'unrelated'
    existing.mkdir()
    with pytest.raises(ValueError, match='not owned'):
        stage_application(release, receipt, existing)


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='Windows release compiler')
def test_compile_small_launcher_without_running_it(tmp_path):
    descriptor = tmp_path / 'bootstrap.json'
    descriptor.write_text(json.dumps({'schema_version': 1, 'mode': 'online',
        'bootstrap_notices': 'Test notices', 'manifest_sha256': 'a'*64,
        'python': {'sha256': 'b'*64, 'url': 'https://example.com/python', 'bundle_path': 'python.zip'},
        'engine': {'sha256': 'c'*64, 'url': 'https://example.com/engine', 'bundle_path': 'engine.zip'}}))
    output = tmp_path / 'FamilyFotoSetup.exe'
    build_exe(Path(__file__).resolve().parents[1], descriptor, output)
    assert output.read_bytes().startswith(b'MZ')


def test_engine_imports_with_stdlib_only(tmp_path):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    doc = document()
    raw = json.dumps(doc).encode()
    (tmp_path / 'legal').mkdir()
    (tmp_path / 'legal/agreement.txt').write_bytes(b'terms')
    archive = tmp_path / 'engine.zip'
    build_engine(root, raw, tmp_path, archive)
    unpacked = tmp_path / 'engine'
    extract_zip(archive, unpacked, 2000000)
    subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
                    'import sys; sys.path.insert(0,sys.argv[1]); '
                    'import installer.windows.online_setup; import installer.common.materialize',
                    str(unpacked)], check=True, capture_output=True, timeout=30)


def test_release_audit_requires_complete_payloads(tmp_path):
    doc = document()
    with pytest.raises(ValueError, match='setup_url'):
        audit_windows_release(manifest(doc), tmp_path)
    doc.update(setup_url='https://github.com/joeandblack/FamilyFotoPub/releases/download/test-1/FamilyFotoSetup.exe',
               minimum_os='10.0.22000')
    with pytest.raises(ValueError, match='Missing core'):
        audit_windows_release(manifest(doc), tmp_path)
    items = [component(key) for key in ['application', 'python', 'postgres-installer', 'postgres-binaries', 'ffmpeg']]
    for role in ('core', 'people-view'):
        item = component('lock-' + role, lock_role=role)
        item['bundle_path'] = f'locks/{role}.txt'
        items.append(item)
    doc['components'] = items
    doc['features']['core'] = [item['id'] for item in items]
    for item in items:
        path = tmp_path / item['bundle_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'payload')
    (tmp_path / 'legal').mkdir()
    (tmp_path / 'legal/agreement.txt').write_bytes(b'terms')
    audit_windows_release(manifest(doc), tmp_path)
    (tmp_path / items[0]['bundle_path']).write_bytes(b'tamper')
    with pytest.raises(ValueError, match='differs'):
        audit_windows_release(manifest(doc), tmp_path)
