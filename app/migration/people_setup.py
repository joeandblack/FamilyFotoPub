"""Set up this computer's face engine on an activated recovered library, reusing its People catalog.

A library recovered from another computer keeps its People data in the one catalog (schema
`people`). Its saved faces can be viewed at once, but scanning needs this computer's own face
engine. This installs it, points the library's profile at the existing catalog, and runs the
library check once, so a failure shows here and not in a worker's log:

    python -m app.migration.people_setup --directory RECOVERY --research-only

Run it after `commit-activation`, with the recovered database running and the server stopped.
`--installation` instead sets up the library a move put into this installation's own folders
(app.migration.move_in), with its database running and the server stopped.
It never scans, never starts the server, and never makes a second catalog. A library recovered
from before the catalog migration is refused with the command to run first.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from .launch import configure_environment


class Refused(ValueError):
    pass


def require_catalog(db):
    """The library's own catalog must exist; an empty second one would hide its People."""
    from face_detect import catalog
    if catalog.exists(db):
        return
    older = [name for name in catalog.people_schemas(db) if name != catalog.SCHEMA]
    if older:
        raise Refused('This library has People data from before the one catalog. Run '
                      f'`python -m face_detect.cli.catalog migrate --from {older[0]}` on the old '
                      'computer, take a new backup, and recover that instead')
    raise Refused('This library has no People catalog')


def active_profile():
    from app.config import configuration_root
    faces = json.loads((configuration_root() / 'local.json').read_text(encoding='utf-8')).get('face_detection') or {}
    return faces.get('active_profile')


def restore_profile(name):
    """Put the library's earlier active face profile back, leaving everything else as it is."""
    from app.config import configuration_root
    from app.telemetry import write_json
    path = configuration_root() / 'local.json'
    cfg = json.loads(path.read_text(encoding='utf-8'))
    faces = cfg.setdefault('face_detection', {})
    if name is None:
        faces.pop('active_profile', None)
    else:
        faces['active_profile'] = name
    write_json(path, cfg)
    path.chmod(0o600)


def rebuild_thumbnails(limit=None):
    """Make the face thumbnails a backup leaves out, from the library's photos. Resumable."""
    import psycopg
    from psycopg.rows import dict_row
    from app.config import DATA, load_config
    from face_detect import catalog
    from face_detect.catalog.thumbnails import rebuild
    cfg = load_config()

    def connect():
        return psycopg.connect(**catalog.database(cfg), row_factory=dict_row, connect_timeout=5)

    return rebuild(connect, cfg['destination']['root'], DATA, limit,
                   progress=lambda counts: print(json.dumps(counts), file=sys.stderr))


def run(research_only, port=None, check=True, thumbnails=False, limit=None):
    """Install the engine, adopt the catalog, check the library. Returns a report; the engine
    check's failure is reported, never raised, so the caller can show why."""
    from app import database
    from app.config import load_config
    from face_detect.cli.conformance import check_this_library, record_certificate, stored_vectors
    from face_detect.cli.prepare import prepare
    from face_detect.cli.setup import setup
    from face_detect.service import create_service

    cfg = load_config()
    with database.connection(cfg['database']) as db:
        require_catalog(db)
    previous = active_profile()
    report = {'profile': setup('insightface', research_only=research_only, activate=True)}
    try:
        report.update(prepare(port))
    except ValueError as refusal:
        # Setup has made the new engine the active profile; a refused library must not keep it.
        restore_profile(previous)
        raise Refused(f'{refusal}. The engine was installed but is not the active profile') from None
    if check and not stored_vectors(load_config()):
        # People is on but has found no faces yet: nothing to compare, and the first scan may
        # write (certify.may_write), so there is no check to pass or record.
        check, report['library_check'] = False, 'no-stored-faces'
    if not check:
        report.setdefault('library_check', 'skipped')
        if thumbnails:
            report['thumbnails'] = rebuild_thumbnails(limit)
        return report
    service = create_service(load_config())
    try:
        result = check_this_library(service, load_config())
    finally:
        service.close()
    record_certificate(load_config(), result)
    report['library_check'] = {key: result.get(key) for key in
                               ('passed', 'reason', 'sampled', 'lowest_sample_score', 'compared', 'unmatched', 'unreadable',
                                'worst', 'median') if key in result}
    if thumbnails:
        report['thumbnails'] = rebuild_thumbnails(limit)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    place = parser.add_mutually_exclusive_group(required=True)
    place.add_argument('--directory', type=Path, help='A recovery folder, set up where it is')
    place.add_argument('--installation', action='store_true',
                       help="This installation's own library, after a move into it")
    parser.add_argument('--research-only', action='store_true',
                        help='Confirm the model terms: non-commercial research use')
    parser.add_argument('--port', type=int, help='Port for the People service (default: the usual one)')
    parser.add_argument('--skip-check', action='store_true',
                        help='Do not run the library check now; the first Discover people runs it')
    parser.add_argument('--rebuild-thumbnails', action='store_true',
                        help='Then make the face thumbnails a backup leaves out, from the photos '
                             '(reads every photo; stop and run again to resume)')
    parser.add_argument('--limit', type=int, help='With --rebuild-thumbnails: stop after this many photos')
    parser.add_argument('--report', type=Path, help='Also write the report to this new file')
    args = parser.parse_args(argv)
    if args.installation:
        if any(os.environ.get(name) for name in ('FAMILYFOTO_CONFIG_DIR', 'FAMILYFOTO_DATA_DIR')):
            parser.error('--installation uses this installation\'s own folders; unset FAMILYFOTO_CONFIG_DIR and FAMILYFOTO_DATA_DIR')
    else:
        # The recovered library's folders must be selected before anything imports app.config.
        configure_environment(args.directory)
    try:
        report = run(args.research_only, args.port, check=not args.skip_check,
                     thumbnails=args.rebuild_thumbnails, limit=args.limit)
    except Refused as refusal:
        print(f'Refused: {refusal}', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str))
    if args.report:
        with args.report.open('x', encoding='utf-8') as output:
            json.dump(report, output, default=str)
    check = report.get('library_check')
    return 1 if isinstance(check, dict) and not check.get('passed') else 0


if __name__ == '__main__':
    sys.exit(main())
