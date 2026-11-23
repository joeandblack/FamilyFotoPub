"""Move a per-model People catalog into the one People catalog, `people`.

    python -m face_detect.cli.catalog migrate --from face_native_<token> [--data DIR]

It runs no model and leaves the source schema as it was. It needs the web server stopped,
because it takes the installation's writer lease. The database work is one transaction:
if any check fails, nothing is created. The face thumbnails are copied afterwards, to
data/people/thumbnails; if that step fails, `thumbnails --from ...` repeats it alone.
Last, the profile's runtime.json is pointed at the catalog (`schema: people`), with the old
schema kept as `previous_schema`: putting it back is the rollback, before any edit is made in
the new catalog. Only counts and identities are printed.

    python -m face_detect.cli.catalog thumbnails --from face_native_<token> [--data DIR]

Face thumbnails aren't in backups. After a restore, rebuild them from the photos (read-only;
it skips those already there, so it can be stopped and run again):

    python -m face_detect.cli.catalog rebuild-thumbnails [--data DIR] [--limit PHOTOS]
"""
import argparse
import json
from pathlib import Path
import sys

from face_detect.catalog import migrate as catalog


def point_profile(data_root, source, space_id):
    """Point the source profile's runtime.json at the one catalog. Returns what it changed."""
    from app.telemetry import write_json
    from face_detect.catalog import SCHEMA
    path = Path(data_root) / 'face_detect/profiles' / source.removeprefix('face_native_') / 'runtime.json'
    if not path.is_file():
        return {'changed': False, 'reason': 'no profile runtime.json'}
    runtime = json.loads(path.read_text(encoding='utf-8'))
    if runtime.get('schema') == SCHEMA:
        return {'changed': False, 'reason': 'already points at the catalog'}
    if runtime.get('schema') != source:
        raise catalog.MigrationRefused('The profile names another schema; it was left as it was')
    write_json(path, {**runtime, 'schema': SCHEMA, 'previous_schema': source,
                      **({'space_id': space_id} if space_id else {})})
    return {'changed': True, 'previous_schema': source}


def main(argv=None, connect=None, data_root=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('migrate', 'thumbnails'):
        command = commands.add_parser(name)
        command.add_argument('--from', dest='source', required=True)
        command.add_argument('--data', type=Path)
    rebuild = commands.add_parser('rebuild-thumbnails')
    rebuild.add_argument('--data', type=Path)
    rebuild.add_argument('--limit', type=int, help='Stop after this many photos')
    args = parser.parse_args(argv)
    if args.command != 'rebuild-thumbnails' and not catalog.SOURCE.fullmatch(args.source):
        parser.error('--from must name a face_native_<token> catalog')
    if data_root is None:
        from app.config import DATA
        data_root = args.data or DATA
    if args.command == 'rebuild-thumbnails':
        from app.config import load_config
        from face_detect import catalog as people_catalog
        from face_detect.catalog.thumbnails import rebuild as rebuild_thumbnails
        cfg = load_config()
        if connect is None:
            import psycopg
            from psycopg.rows import dict_row
            def connect():
                return psycopg.connect(**people_catalog.database(cfg), row_factory=dict_row, connect_timeout=5)
        print(json.dumps(rebuild_thumbnails(connect, cfg['destination']['root'], data_root, args.limit,
                                            progress=lambda c: print(json.dumps(c), file=sys.stderr)), indent=2))
        return 0
    result = {}
    try:
        if args.command == 'migrate':
            if connect is None:
                from app import database
                connect = database.connection
            with connect() as db:
                result = catalog.migrate(db, args.source)
                db.commit()
        result.update(catalog.copy_thumbnails(data_root, args.source))
        result['runtime'] = point_profile(data_root, args.source, result.get('active_space_id'))
    except catalog.MigrationRefused as refusal:
        # An expected refusal: say why, without a traceback. Nothing was changed.
        print(f'Refused: {refusal}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
