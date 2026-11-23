import csv
import json
import os
import subprocess

import pytest

from installer.windows.restore import restore_desktop_access


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows NTFS permissions')


def test_restored_jobs_and_caches_get_desktop_sid_only_inside_runtime_trees(tmp_path, monkeypatch):
    from app.backup.diagnostics import ExplainedFailure

    data = tmp_path / 'data'
    setup = data / 'setup'
    setup.mkdir(parents=True)
    sid = next(csv.reader(subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'])
                          .decode(errors='replace').splitlines()))[1]
    journal = setup / 'install.json'
    journal.write_text(json.dumps({'owner': sid, 'root': str(tmp_path)}))
    home = tmp_path / 'restored-library'
    media = home / 'media-copy/media'
    media.mkdir(parents=True)
    (media / 'photo.jpg').write_bytes(b'original bytes')
    subprocess.run(['icacls', media / 'photo.jpg', '/inheritance:r', '/grant:r', '*S-1-3-4:F'], check=True)
    (tmp_path / 'restore').mkdir()
    (tmp_path / 'config').mkdir()
    destination = {'root': str(media), 'drive': 'D', 'serial': 'disk', 'volume_serial': 'volume', 'volume_id': 'id'}
    (tmp_path / 'restore/MOVE-IN.json').write_text(json.dumps({
        'plan': {'media_directory': str(home), 'backup': str(tmp_path / 'backup')},
        'results': {'media_root': str(media)}}))
    (tmp_path / 'config/local.json').write_text(json.dumps({'destination': destination}))
    monkeypatch.setattr('app.migration.disk.disk_binding', lambda path: destination)
    names = ['.recovery-admission/copy.lock', 'thumbnails/00/aa/photo.jpg',
             'people/thumbnails/face.jpg', 'face_detect/profiles/test/jobs/import-2/import.json']
    for name in names + ['postgres/control']:
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    outside = data / 'postgres/control'
    saved = tmp_path / 'acl'

    def database_acl():
        subprocess.run(['icacls', outside, '/save', saved], check=True)
        return saved.read_bytes()

    before = database_acl()
    journal.write_text(json.dumps({'owner': 'wrong', 'root': str(tmp_path)}))
    with pytest.raises(ExplainedFailure, match='account'):
        restore_desktop_access(tmp_path)
    journal.write_text(json.dumps({'owner': sid, 'root': str(tmp_path)}))
    linked = data / 'thumbnails/link'
    os.link(outside, linked)
    with pytest.raises(ExplainedFailure, match='Linked'):
        restore_desktop_access(tmp_path)
    linked.unlink()
    # Model a protected restored leaf with an owner ACE but no desktop-user ACE.
    job = data / names[-1]
    subprocess.run(['icacls', job, '/inheritance:r', '/grant:r', '*S-1-3-4:F'], check=True)
    subprocess.run(['icacls', job, '/save', saved], check=True)
    assert sid not in saved.read_text(encoding='utf-16-le')
    from installer.windows import restore_acl
    original_grant = restore_acl.grant
    attempts = []

    def while_pinned(handle, directory, owner):
        if not directory and not attempts:
            # The first tree contains copy.lock. Try a harmless rename while the
            # checked handle is open, at the point where permissions are applied.
            lock = data / names[0]
            with pytest.raises(PermissionError) as blocked:
                lock.rename(lock.with_suffix('.moved'))
            assert blocked.value.winerror == 32
            attempts.append(True)
        original_grant(handle, directory, owner)

    monkeypatch.setattr(restore_acl, 'grant', while_pinned)
    restore_desktop_access(tmp_path)
    assert attempts
    assert database_acl() == before
    assert (media / 'photo.jpg').read_bytes() == b'original bytes'
    subprocess.run(['icacls', media / 'photo.jpg', '/save', saved], check=True)
    assert f';0x1301bf;;;{sid})' in saved.read_text(encoding='utf-16-le')
    for name in names:
        subprocess.run(['icacls', data / name, '/save', saved], check=True)
        assert f';0x1301bf;;;{sid})' in saved.read_text(encoding='utf-16-le')
    # A relocated/mismatched config must fail before even the runtime ACL pass.
    monkeypatch.setattr(restore_acl, 'repair_trees', lambda *args: pytest.fail('must validate first'))
    destination['root'] = str(tmp_path / 'unrelated')
    (tmp_path / 'config/local.json').write_text(json.dumps({'destination': destination}))
    with pytest.raises(ExplainedFailure, match='location'):
        restore_desktop_access(tmp_path)


def test_desktop_lease_blocks_repair_until_server_releases_it(tmp_path):
    from app.backup.admission import participant
    from app.backup.diagnostics import ExplainedFailure
    from installer.windows.restore_acl import repair_trees

    sid = next(csv.reader(subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'])
                          .decode(errors='replace').splitlines()))[1]
    with participant(tmp_path):
        with pytest.raises(ExplainedFailure, match='stop the FamilyFoto desktop server') as blocked:
            repair_trees(tmp_path, sid)
        assert blocked.value.__cause__.winerror == 32
    repair_trees(tmp_path, sid)


def test_media_repair_refuses_link_and_preserves_unrelated_files(tmp_path):
    from installer.windows.restore_acl import repair_media
    from app.backup.diagnostics import ExplainedFailure
    sid = next(csv.reader(subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'])
                          .decode(errors='replace').splitlines()))[1]
    home = tmp_path / 'library'
    media = home / 'media-copy/media'
    media.mkdir(parents=True)
    outside = tmp_path / 'backup-photo'
    outside.write_bytes(b'keep')
    saved = tmp_path / 'acl'
    subprocess.run(['icacls', outside, '/save', saved], check=True)
    before = saved.read_bytes()
    os.link(outside, media / 'linked')
    with pytest.raises(ExplainedFailure, match='Linked'):
        repair_media(home, sid)
    subprocess.run(['icacls', outside, '/save', saved], check=True)
    assert saved.read_bytes() == before
    assert outside.read_bytes() == b'keep'


@pytest.mark.parametrize('accepted', [False, True])
def test_unprotected_media_requires_recorded_acceptance(tmp_path, monkeypatch, accepted):
    from installer.windows import restore_acl
    from app.backup.diagnostics import ExplainedFailure
    sid = next(csv.reader(subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'])
                          .decode(errors='replace').splitlines()))[1]
    monkeypatch.setattr('app.backup.volumes.windows_acls_persist', lambda path: False)
    monkeypatch.setattr(restore_acl, 'grant', lambda *args: pytest.fail('no ACLs on exFAT'))
    if accepted:
        restore_acl.repair_media(tmp_path, sid, allow_unprotected=True)
    else:
        with pytest.raises(ExplainedFailure, match='cannot retain'):
            restore_acl.repair_media(tmp_path, sid)
