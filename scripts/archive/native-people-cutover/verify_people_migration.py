"""Verify the candidate's original tables and preserve its saved thumbnails."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil

import psycopg

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # prepare_people_migration stays in scripts/
from prepare_people_migration import fingerprint, save


def copy_thumbnail(source, destination):
    if source.is_symlink() or not source.stem.isdecimal():
        raise ValueError('Unexpected thumbnail path')
    target = destination/source.name
    shutil.copy2(source, target)
    with source.open('rb') as stream:
        expected = hashlib.file_digest(stream, 'sha256').hexdigest()
    with target.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise ValueError('Thumbnail copy differs')
    return source.name, expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--migration', type=Path, required=True)
    parser.add_argument('--copy-thumbnails', action='store_true')
    args = parser.parse_args()
    folder = args.migration.resolve()
    report = json.loads((folder/'migration.json').read_text())
    settings = json.loads((folder/'candidate-connection.json').read_text())
    if (settings['host'], settings['port'], settings['dbname']) != (
        '127.0.0.1', 55449, report['candidate_database']
    ) or not settings['dbname'].startswith('familyfoto_people_'):
        raise ValueError('Only the isolated migration database is permitted')
    with psycopg.connect(**settings) as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if Path(db.execute('SHOW data_directory').fetchone()[0]).resolve() != folder.parent/'validation-postgres':
            raise ValueError('Unexpected migration cluster')
        actual = fingerprint(db, report['source_schema'])
    for table, expected in report['source_tables'].items():
        if actual.get(table) != expected:
            raise RuntimeError('Preserved table changed: '+table)
    verification = {'preserved_tables': len(report['source_tables']), 'original_records_unchanged': True}
    if args.copy_thumbnails:
        source = Path(report['source_profile'])/'thumbnails'
        destination = folder/'thumbnails'
        destination.mkdir(exist_ok=True)
        if source.is_symlink() or destination.is_symlink():
            raise ValueError('Linked thumbnail directories are not permitted')
        with ThreadPoolExecutor(max_workers=8) as pool:
            hashes = dict(pool.map(lambda path: copy_thumbnail(path, destination), source.glob('*.jpg')))
        save(folder/'thumbnail-hashes.json', hashes)
        verification['verified_thumbnails'] = len(hashes)
    save(folder/'preservation-verification.json', verification)
    print(json.dumps(verification))


if __name__ == '__main__':
    main()
