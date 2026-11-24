import json
import pytest
from pathlib import Path
from app import release

A = 'a' * 40
B = 'b' * 40


def project(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'pyproject.toml').write_text(Path('pyproject.toml').read_text(encoding='utf-8'), encoding='utf-8')
    return root


def info(root):
    release.build_info.cache_clear()
    try:
        return release.build_info(root)
    finally:
        release.build_info.cache_clear()


def test_branch_from_loose_ref_and_packed_refs(tmp_path):
    root = project(tmp_path / 'checkout')
    (root / '.git/refs/heads/feature').mkdir(parents=True)
    (root / '.git/HEAD').write_text('ref: refs/heads/feature/x\n')
    (root / '.git/refs/heads/feature/x').write_text(A + '\n')
    result = info(root)
    assert result['commit'] == A and result['branch'] == 'feature/x' and not result['dirty']
    assert result['version'] == release.metadata()['application_version']
    (root / '.git/refs/heads/feature/x').unlink()
    (root / '.git/packed-refs').write_text(f'# pack-refs with: peeled\n{B} refs/heads/feature/x\n')
    assert info(root)['commit'] == B


def test_detached_head_and_worktree_file(tmp_path):
    common = tmp_path / 'main/.git'
    (common / 'worktrees/wt').mkdir(parents=True)
    (common / 'worktrees/wt/HEAD').write_text(A + '\n')
    (common / 'worktrees/wt/commondir').write_text('../..\n')
    root = project(tmp_path / 'wt')
    (root / '.git').write_text(f'gitdir: {common / "worktrees/wt"}\n')
    assert (info(root)['commit'], info(root)['branch']) == (A, '')
    (common / 'worktrees/wt/HEAD').write_text('ref: refs/heads/main\n')
    (common / 'refs/heads').mkdir(parents=True)
    (common / 'refs/heads/main').write_text(B)
    assert (info(root)['commit'], info(root)['branch']) == (B, 'main')


def test_release_folder_uses_manifest_without_git(tmp_path):
    root = project(tmp_path / 'release')
    (root / 'release-manifest.json').write_text(json.dumps({'source_commit': A, 'dirty': True}))
    result = info(root)
    assert result['commit'] == A and result['dirty'] is True and result['branch'] == ''


def test_unreadable_or_odd_values_give_blanks(tmp_path):
    root = project(tmp_path / 'odd')
    (root / '.git').mkdir()
    assert info(root)['commit'] == ''  # No HEAD
    (root / '.git/HEAD').write_text('not a commit <script>')
    assert info(root)['commit'] == ''
    (root / '.git/HEAD').write_text('ref: refs/heads/<b>\n')
    assert info(root)['branch'] == ''
    bare = tmp_path / 'nothing'
    bare.mkdir()
    assert info(bare) == {'version': 'unknown', 'commit': '', 'branch': '', 'dirty': False}


def test_label_is_the_code_at_server_start_not_files_changed_before_the_first_request(monkeypatch):
    from app.api import create_app
    from app.system_api import version
    if not (release.Path(release.__file__).resolve().parents[1] / '.git').exists():
        pytest.skip('Runs from a Git checkout')
    disk = {'head': A}           # What .git says right now
    monkeypatch.setattr(release, '_checkout_head', lambda root: (disk['head'], 'main'))
    release.build_info.cache_clear()
    try:
        create_app()             # Server start: commit A is running
        disk['head'] = B         # The checkout moves to B before anyone opens a page
        assert version()['commit'] == A
    finally:
        release.build_info.cache_clear()
