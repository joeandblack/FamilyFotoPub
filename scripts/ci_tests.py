"""CI change selection and disjoint pytest groups; ordinary local pytest is unchanged."""
import argparse
import ast
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ('core', 'database', 'swift', 'apfs')
DATABASE_SIGNALS = ('FAMILYFOTO_INTEGRATION', 'FAMILYFOTO_BACKUP_PG_BIN',
                    'FAMILYFOTO_BACKUP_TEST_PORT', 'psycopg.connect(', 'database.connection(')


@lru_cache(maxsize=1)
def database_modules():
    """Follow shared fixture imports so a consumer cannot fall into the no-DB job.

    Whole modules stay together: some contain both pure and integration tests.
    Conservative extra DB classification is preferable to silently skipping a fixture.
    """
    files = {p.relative_to(ROOT).as_posix(): p for folder in ('tests', 'face_detect/tests', 'app/fastbackup/tests')
             for p in (ROOT / folder).glob('*.py')}
    aliases = {}
    for name in files:
        aliases[name[:-3].replace('/', '.')] = name
        aliases[Path(name).stem] = name
    dependencies, required = {}, set()
    for name, path in files.items():
        source = path.read_text(encoding='utf-8')
        if name != 'tests/test_ci_tests.py' and any(signal in source for signal in DATABASE_SIGNALS):
            required.add(name)
        dependencies[name] = set()
        for node in ast.walk(ast.parse(source)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ''] + [(node.module + '.' if node.module else '') + a.name for a in node.names]
            dependencies[name].update(aliases[m] for m in modules if m in aliases)
    while True:
        expanded = required | {name for name, imports in dependencies.items() if imports & required}
        if expanded == required:
            return frozenset(required)
        required = expanded


def group_for(nodeid, markers=()):
    for group in ('swift', 'apfs', 'database'):
        if 'ci_' + group in markers:
            return group
    return 'database' if nodeid.split('::')[0] in database_modules() else 'core'


def shard_for(nodeid, count):
    # Keep tests sharing a module's fixtures together, on separate runner VMs.
    return int.from_bytes(hashlib.sha256(nodeid.split('::')[0].encode()).digest()[:8], 'big') % count


def plan(paths, full=False):
    result = {group: bool(full) for group in ('database', 'swift', 'apfs')}
    for path in paths:
        common = (path.startswith(('.github/', 'installer/locks/', 'face_detect/config/dependencies/'))
                  or path.startswith('scripts/ci_')
                  or (path.startswith('tests/') and not Path(path).name.startswith('test_')) or path.startswith('requirements')
                  or path in ('pyproject.toml', 'pytest.ini', 'tests/conftest.py', 'scripts/ci_tests.py', 'tests/test_ci_tests.py'))
        if common:
            result = dict.fromkeys(result, True)
        if path.startswith(('app/', 'face_detect/', 'databases/', 'config/', 'installer/', 'scripts/')) or path in database_modules():
            result['database'] = True
        if path.startswith(('installer/macos/', 'installer/common/', 'app/backup/', 'app/migration/')) or path in (
                'tests/test_mac_launcher.py', 'tests/test_macos_setup_window.py', 'app/config.py'):
            result['swift'] = True
        if path.startswith(('app/backup/', 'app/fastbackup/', 'app/storage/', 'app/migration/', 'installer/macos/', 'installer/common/')) or path in (
                'tests/test_backup_capture_retry.py', 'tests/test_backup_restore_remount.py',
                'tests/test_macos_native_media.py'):
            result['apfs'] = True
        if path.startswith(('tests/', 'face_detect/tests/', 'app/fastbackup/tests/')) and (ROOT / path).is_file():
            source = (ROOT / path).read_text(encoding='utf-8')
            for group in result:
                if 'ci_' + group in source:
                    result[group] = True
        # Unknown new areas default to full qualification, not an accidental bypass.
        if ('/' not in path and Path(path).suffix in ('.py', '.sh', '.ps1')) or ('/' in path and not path.startswith(('app/', 'face_detect/', 'databases/', 'config/', 'installer/',
                                                'scripts/', 'tests/', 'web/', 'docs/', '.github/'))):
            result = dict.fromkeys(result, True)
    return result


def pytest_addoption(parser):
    parser.addoption('--ci-group', choices=GROUPS)
    parser.addoption('--ci-shard', type=int, default=0)
    parser.addoption('--ci-shards', type=int, default=1)
    parser.addoption('--ci-report', help='Write the complete collected partition as JSON')


def pytest_configure(config):
    for group in ('database', 'swift', 'apfs'):
        config.addinivalue_line('markers', f'ci_{group}: isolated {group} CI job')


def pytest_collection_modifyitems(config, items):
    import pytest
    group = config.getoption('--ci-group')
    shard, count = config.getoption('--ci-shard'), config.getoption('--ci-shards')
    if count < 1 or not 0 <= shard < count:
        raise pytest.UsageError('CI shard must be in [0, --ci-shards)')
    partition = {item.nodeid: group_for(item.nodeid, {m.name for m in item.iter_markers()}) for item in items}
    if config.getoption('--ci-report'):
        Path(config.getoption('--ci-report')).write_text(json.dumps(partition, indent=2) + '\n')
    if group:
        selected, deselected = [], []
        for item in items:
            (selected if partition[item.nodeid] == group and shard_for(item.nodeid, count) == shard else deselected).append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    full = args.full
    paths = []
    if not full:
        try:
            base = subprocess.check_output(['git', 'merge-base', args.base or '', args.head], text=True).strip()
            paths = subprocess.check_output(['git', 'diff', '--no-renames', '--name-only', '-z', base, args.head]).decode().split('\0')
        except subprocess.CalledProcessError:
            full = True  # Missing history / first push must never skip qualification.
    result = plan([p for p in paths if p], full)
    print(json.dumps({'full': full, 'changed_paths': paths, 'groups': result}, indent=2))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for name, enabled in result.items():
                print(f'{name}={str(enabled).lower()}', file=output)


if __name__ == '__main__':
    main()
