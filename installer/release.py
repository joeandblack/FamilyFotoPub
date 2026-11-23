"""Build a tracked, allowlisted application ZIP without private installation state."""
import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tomllib
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def selected_files(root, tracked):
    rules = json.loads((root / 'installer/release-files.json').read_text(encoding='utf-8'))
    explicit = set(rules['files'] + rules['scripts'])
    selected = set()
    for name in tracked:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise ValueError('Unsafe tracked filename')
        if any(part in rules['exclude_directories'] for part in path.parts):
            continue
        if name in explicit or path.suffix in rules['trees'].get(path.parts[0], []):
            local = root / name
            for component in (local, *local.parents):
                if component == root:
                    break
                if component.is_symlink() or component.is_junction():
                    raise ValueError(f'Linked release input refused: {name}')
            if not local.is_file():
                raise ValueError(f'Missing release input: {name}')
            selected.add(name)
    missing = explicit - selected
    if missing:
        raise ValueError(f'Missing required release files: {sorted(missing)}')
    return sorted(selected)


def validate(root, names):
    document = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    project = document['project']
    requirements = (root / 'requirements.txt').read_text().splitlines()
    if project['dependencies'] != [line.strip() for line in requirements if line.strip()]:
        raise ValueError('Runtime dependencies differ between pyproject.toml and requirements.txt')
    versions = []
    for name in names:
        if not name.startswith('databases/'):
            continue
        match = re.fullmatch(r'databases/(\d{3})_[a-z0-9_]+\.sql', name)
        if not match:
            raise ValueError(f'Invalid schema resource filename: {name}')
        versions.append(int(match[1]))
    versions.sort()
    target = document['tool']['familyfoto']['release']['core-schema-version']
    if versions != list(range(1, target + 1)):
        raise ValueError('Release schema version does not match packaged SQL history')
    entries = document['tool']['familyfoto']['entrypoints']
    modules = entries['modules'] + [value.split(':')[0] for value in entries['asgi']]
    for module in modules:
        path = module.replace('.', '/')
        # A module file, or a package that `python -m` can run (it has __main__.py).
        if path + '.py' not in names and path + '/__main__.py' not in names:
            raise ValueError(f'Missing entry point: {module}')
    for name in entries['scripts']:
        if name not in names:
            raise ValueError(f'Missing launch script: {name}')
    return document


def build(root, output, allow_dirty=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    # Include staged additions, but never untracked files or local runtime state.
    tracked = git('ls-files', '-z').decode('utf-8').strip('\0').split('\0')
    dirty = bool(git('status', '--porcelain', '--untracked-files=no').strip())
    if dirty and not allow_dirty:
        raise ValueError('Commit tracked changes before release (or --allow-dirty for local tests)')
    names = selected_files(root, tracked)
    document = validate(root, names)
    version = document['project']['version']
    if not version or any(c not in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-' for c in version):
        raise ValueError('Unsafe release version')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'FamilyFoto-{version}.zip'
    manifest = {'format_version': 1, 'application_version': version,
                'core_schema_version': document['tool']['familyfoto']['release']['core-schema-version'],
                'source_commit': git('rev-parse', 'HEAD').decode().strip(),
                'dirty': dirty, 'files': {}}
    checksum = archive.with_suffix('.zip.sha256')
    owned = []
    try:
        # Reserve both outputs exclusively. On handled failure remove only files
        # this invocation created, never a pre-existing archive or checksum.
        with checksum.open('x', encoding='utf-8') as receipt:
            owned.append(checksum)
            with archive.open('xb') as stream:
                owned.append(archive)
                with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as package:
                    def write(name, data):
                        item = zipfile.ZipInfo('FamilyFoto/' + name, date_time=(2020, 1, 1, 0, 0, 0))
                        item.compress_type = zipfile.ZIP_DEFLATED
                        item.create_system = 3
                        item.external_attr = (stat.S_IFREG | (0o755 if name.endswith('.sh') else 0o644)) << 16
                        package.writestr(item, data)
                    for name in names:
                        data = (root / name).read_bytes()
                        manifest['files'][name] = {'sha256': digest(data), 'bytes': len(data)}
                        write(name, data)
                    write('release-manifest.json', json.dumps(manifest, indent=2, sort_keys=True).encode())
            receipt.write(digest(archive.read_bytes()) + '  ' + archive.name + '\n')
    except BaseException:
        for path in reversed(owned):
            path.unlink(missing_ok=True)
        raise
    return archive


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format_version') != 1:
        raise ValueError('Unsupported release manifest format')
    for name, expected in manifest['files'].items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise ValueError('Unsafe manifest filename')
        local = (root / name).resolve()
        if not local.is_relative_to(root):
            raise ValueError('Manifest path escapes release')
        data = local.read_bytes()
        if len(data) != expected['bytes'] or digest(data) != expected['sha256']:
            raise ValueError(f'Release file differs: {name}')
    document = validate(root, list(manifest['files']))
    if manifest['application_version'] != document['project']['version']:
        raise ValueError('Release version differs from manifest')
    if manifest['core_schema_version'] != document['tool']['familyfoto']['release']['core-schema-version']:
        raise ValueError('Release schema differs from manifest')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('build')
    create.add_argument('--output', type=Path, required=True)
    create.add_argument('--allow-dirty', action='store_true')
    check = sub.add_parser('verify')
    check.add_argument('root', type=Path)
    args = parser.parse_args()
    if args.action == 'build':
        print(build(Path(__file__).resolve().parents[1], args.output, args.allow_dirty))
    else:
        print(json.dumps(verify(args.root), indent=2))


if __name__ == '__main__':
    main()
