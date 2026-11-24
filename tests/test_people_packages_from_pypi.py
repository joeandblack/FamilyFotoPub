"""People's provider libraries come from PyPI by hash, never from a release (#20, beta.4)."""
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from installer.common import local_payloads
from installer.common.manifest import Manifest, sha256

ROOT = Path(__file__).resolve().parents[1]


def receipt(tmp_path, monkeypatch, locks):
    lock_map = {}
    for role in locks:
        path = tmp_path / f'{role}.txt'
        path.write_text('pkg==1.0 --hash=sha256:' + '0' * 64 + '\n')
        lock_map[role] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (tmp_path / 'wheels').mkdir(exist_ok=True)
    doc = {'schema_version': 1, 'stage': 'materialized', 'wheelhouse': str(tmp_path / 'wheels'), 'locks': lock_map}
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(doc))
    monkeypatch.setenv('FAMILYFOTO_PAYLOAD_RECEIPT', str(path))


@pytest.mark.parametrize('role', ['insightface', 'facenet'])
def test_provider_roles_fall_back_when_the_release_has_no_lock(tmp_path, monkeypatch, role):
    receipt(tmp_path, monkeypatch, ['core', 'people-view'])
    assert local_payloads.pip_arguments(role) == []


def test_every_other_role_stays_prepared_payloads_only(tmp_path, monkeypatch):
    receipt(tmp_path, monkeypatch, ['core'])
    assert '--no-index' in local_payloads.pip_arguments('core')
    for role in ('password', 'people-view'):
        with pytest.raises(KeyError):
            local_payloads.pip_arguments(role)


def test_an_old_release_that_shipped_provider_locks_still_uses_them(tmp_path, monkeypatch):
    receipt(tmp_path, monkeypatch, ['core', 'insightface'])
    assert '--no-index' in local_payloads.pip_arguments('insightface')


@pytest.mark.parametrize('target,lock', [
    ('windows-amd64', 'face_detect/config/dependencies/insightface-windows-amd64.txt'),
    ('macos-arm64', 'installer/locks/insightface-macos-arm64.txt'),
])
def test_setup_installs_providers_from_pypi_by_hash(tmp_path, monkeypatch, target, lock):
    from face_detect.cli import setup
    receipt(tmp_path, monkeypatch, ['core', 'people-view'])
    calls = []
    monkeypatch.setattr(setup.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(setup, 'install_view_dependencies', lambda target: None)
    setup.install_dependencies('insightface', tmp_path / 'env', target)
    install = calls[0]
    assert install[1:4] == ['-m', 'pip', 'install']
    assert '--require-hashes' in install and '--only-binary=:all:' in install and '--no-index' not in install
    assert Path(install[-1]) == ROOT / lock
    # Every requirement in the lock it installs from is hash-pinned.
    lines = [l for l in (ROOT / lock).read_text(encoding='utf-8').splitlines() if l.strip() and not l.startswith('#')]
    assert lines and all('--hash=sha256:' in l for l in lines)


def test_unreachable_pypi_says_to_connect(tmp_path, monkeypatch):
    from face_detect.cli import setup
    receipt(tmp_path, monkeypatch, ['core', 'people-view'])
    def offline(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(setup.subprocess, 'run', offline)
    with pytest.raises(RuntimeError, match='downloaded from pypi.org during setup .* Connect to the internet'):
        setup.install_dependencies('facenet', tmp_path / 'env', 'windows-amd64')


def _people_manifest(with_locks):
    def item(cid, **extra):
        return dict(id=cid, bundle_path=f'payload/{cid}.' + ('txt' if 'lock_role' in extra else 'zip'),
                    byte_size=1, installed_size_estimate=1, sha256='0' * 64, url=f'https://example.com/{cid}',
                    dependencies=[], license_ids=['x'], **extra)
    core = [item(c) for c in ('application', 'python', 'postgres-installer', 'postgres-binaries')]
    core += [item('lock-core', lock_role='core'), item('lock-people-view', lock_role='people-view')]
    people = [item('model-insightface', model_provider='insightface', model_identity='buffalo_l'),
              item('model-facenet', model_provider='facenet', model_identity='vggface2')]
    if with_locks:
        people += [item('lock-insightface', lock_role='insightface'), item('lock-facenet', lock_role='facenet')]
    doc = dict(schema_version=1, installer_version=1, release_id='test-1', source_commit='a' * 40,
               platform='windows-x64', minimum_os='10.0.22000',
               setup_url='https://github.com/joeandblack/FamilyFotoPub/releases/download/test-1/FamilyFotoSetup.exe',
               agreement_path='legal/agreement.txt', agreement_sha256='0' * 64,
               components=core + people,
               features={'core': [c['id'] for c in core], 'people': [c['id'] for c in people]})
    raw = json.dumps(doc).encode()
    return Manifest(raw, sha256(raw))


def test_windows_audit_refuses_shipped_provider_locks(tmp_path):
    from installer.windows.build_shared import audit_windows_release
    with pytest.raises(ValueError, match='installed from PyPI'):
        audit_windows_release(_people_manifest(with_locks=True), tmp_path)
    # Without them the People checks pass (and the audit moves on to the payload bytes).
    with pytest.raises(FileNotFoundError):
        audit_windows_release(_people_manifest(with_locks=False), tmp_path)


def test_terms_say_people_libraries_come_from_pypi():
    text = (ROOT / 'installer/legal/agreement.txt').read_text(encoding='utf-8')
    assert 'pypi.org' in text and 'the face models come with the release' in text
