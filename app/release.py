"""Offline release metadata and core schema compatibility; never opens a database."""
import argparse
import json
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
