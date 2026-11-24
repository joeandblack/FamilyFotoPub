"""Offline release metadata and core schema compatibility; never opens a database."""
import argparse
import functools
import json
import re
from pathlib import Path
import tomllib


def metadata(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    with (root / 'pyproject.toml').open('rb') as stream:
        document = tomllib.load(stream)
    project = document['project']
    release = document['tool']['familyfoto']['release']
    return {
        'application_version': project['version'],
        'requires_python': project['requires-python'],
        'core_schema_version': release['core-schema-version'],
        'minimum_upgrade_schema': release['minimum-upgrade-schema'],
        'distribution': release['distribution'],
    }


def display_version(root=None):
    """Partial legacy code updates must not fail just to display an API version."""
    try:
        return metadata(root)['application_version']
    except FileNotFoundError:
        return 'unknown'


def _checkout_head(root):
    """(commit, branch) of a Git checkout, read from .git without running git.

    Running git on a Mac without Command Line Tools opens an install prompt, so this reads
    HEAD and the ref files directly. A worktree's .git is a file naming its git directory."""
    git = root / '.git'
    if git.is_file():
        git = (root / git.read_text(encoding='utf-8').split('gitdir:', 1)[1].strip()).resolve()
    head = (git / 'HEAD').read_text(encoding='utf-8').strip()
    if not head.startswith('ref: '):
        return head, ''
    ref = head[5:].strip()
    common = git / (git / 'commondir').read_text(encoding='utf-8').strip() if (git / 'commondir').is_file() else git
    branch = ref.removeprefix('refs/heads/')
    for folder in (git, common):
        if (folder / ref).is_file():
            return (folder / ref).read_text(encoding='utf-8').strip(), branch
    packed = common / 'packed-refs'
    for line in packed.read_text(encoding='utf-8').splitlines() if packed.is_file() else ():
        if line.endswith(' ' + ref):
            return line.split(' ', 1)[0], branch
    return '', branch


@functools.cache
def build_info(root=None):
    """What code this server runs, for the sidebar label: version, branch and commit.

    Read once, at the first request after start: changing the files without a restart doesn't
    change the running code, so it doesn't change the label either. A release folder gives its
    manifest's commit (and dirty flag) without hashing every file; anything unreadable gives ''."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    info = {'version': display_version(root), 'commit': '', 'branch': '', 'dirty': False}
    try:
        if (root / '.git').exists():
            info['commit'], info['branch'] = _checkout_head(root)
        elif (root / 'release-manifest.json').is_file():
            manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
            info['commit'], info['dirty'] = manifest.get('source_commit', ''), manifest.get('dirty') is True
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    if not isinstance(info['commit'], str) or not re.fullmatch('[0-9a-f]{40}', info['commit']):
        info['commit'] = ''
    if not re.fullmatch(r'[\w./-]{0,200}', info['branch']):
        info['branch'] = ''
    return info


def schema_compatibility(applied, info=None):
    """Validate the complete version set, not just max(version)."""
    info = info or metadata()
    applied = set(applied)
    if any(type(value) is not int or value < 1 for value in applied):
        raise ValueError('Schema versions must be positive integers')
    current = max(applied, default=0)
    if applied != set(range(1, current + 1)):
        raise ValueError('Schema history has gaps; manual investigation required')
    if current > info['core_schema_version']:
        return 'newer-database-refused'
    if current < info['minimum_upgrade_schema']:
        return 'unsupported-upgrade'
    return 'compatible' if current == info['core_schema_version'] else 'upgrade-required'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema-version', type=int,
                        help='Report compatibility for an assumed contiguous history; no DB access')
    args = parser.parse_args()
    info = metadata()
    if args.schema_version is not None:
        if args.schema_version < 0:
            parser.error('schema version cannot be negative')
        info['compatibility'] = schema_compatibility(range(1, args.schema_version + 1), info)
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
