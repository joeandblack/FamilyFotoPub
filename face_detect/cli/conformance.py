"""Describe this installation's face space, or certify this backend for stored vectors.

Read-only towards the library: it never scans the library, writes to the database or copies
photos. `check-library` reads the People catalog in a read-only transaction and opens a
sample of photos where they are. Only similarity numbers are reported.

    python -m face_detect.cli.conformance describe
    python -m face_detect.cli.conformance check-library [--out certificate.json]

To measure another computer before a move, carry a sample to it. The folder holds the
sampled faces' boxes and vectors and a copy of each sampled photo, and no names. It is
private: keep it out of repositories, and delete it on both computers after the check.

    python -m face_detect.cli.conformance check-library --export-sample NEW_DIR    (old computer)
    python -m face_detect.cli.conformance check-library --sample-from DIR [--out certificate.json]    (new)

A golden set of fixed images is optional, for testing hardware alongside the code. Its
images must be safe to share, never family photos:

    python -m face_detect.cli.conformance make --images DIR --out NEW_DIR --source "origin and licence"
    python -m face_detect.cli.conformance check --golden DIR [--tolerance 0.999] [--out certificate.json]
"""
import argparse
import json
from pathlib import Path
import sys

from app.config import DATA, load_config
from face_detect.service import create_service
from face_detect.spaces import conformance, library_check

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.tif', '.tiff'}

CANDIDATES = '''SELECT d.id, a.content_hash, d.box, d.score
    FROM face_detections d JOIN face_analyses a ON a.id = d.analysis_id
    JOIN embedding e ON e.detection_id = d.id AND e.space_id = %s
    JOIN public.media m ON m.sha256 = a.content_hash AND m.kind = 'image'
    WHERE NOT d.ignored AND d.superseded_by IS NULL AND d.score >= %s
      AND NOT EXISTS (SELECT 1 FROM public.media_trash t WHERE t.media_id = m.id)'''


def read_library(cfg, data_root, size):
    """The stored vectors' model manifest, a sample of good stored faces with their vectors,
    and where each sampled photo is. One read-only transaction; the vectors of the sample only."""
    import psycopg
    from psycopg.rows import dict_row
    from face_detect import catalog
    from face_detect.catalog.spaces import active_space
    with psycopg.connect(**catalog.database(cfg), row_factory=dict_row, connect_timeout=5) as db:
        db.execute('SET default_transaction_read_only = on')
        with db.transaction():
            if not catalog.exists(db):
                raise ValueError('This library has no People catalog yet; run the catalog migration first')
            model = active_space(db)
            if model is None:
                raise ValueError('This library has no stored vectors to check against yet')
            manifest = db.execute('SELECT manifest FROM space WHERE space_id = %s', (model,)).fetchone()['manifest']
            candidates = db.execute(CANDIDATES, (model, library_check.LOWEST_SCORE)).fetchall()
            samples = library_check.choose_samples(
                [{'id': row['id'], 'content_hash': row['content_hash'], 'box': row['box'], 'score': row['score']}
                 for row in candidates], size)
            ids = [face['id'] for face in samples]
            vectors = {row['detection_id']: row['vector'] for row in db.execute(
                'SELECT detection_id, vector FROM embedding WHERE space_id = %s AND detection_id = ANY(%s)',
                (model, ids))}
            media = db.execute(
                "SELECT sha256, relative_path, coalesce(stored_sha256, sha256) AS file_sha256 FROM public.media"
                " WHERE kind = 'image' AND sha256 = ANY(%s)", ([face['content_hash'] for face in samples],)).fetchall()
    for face in samples:
        face['vector'] = vectors[face['id']]
    root = Path(cfg['destination']['root'])
    return (manifest, samples, {row['sha256']: photo_path(root, row['relative_path']) for row in media},
            {row['sha256']: row['file_sha256'] for row in media})


def photo_path(root, relative):
    """The photo as this disk spells its name. A name written on Windows is usually NFC, and
    a Mac may list it decomposed; try the stored spelling first."""
    import unicodedata
    for form in (None, 'NFC', 'NFD'):
        candidate = root / (relative if form is None else unicodedata.normalize(form, relative))
        if candidate.is_file():
            return candidate
    return root / relative


def check_this_library(service, cfg, data_root=DATA, size=library_check.SAMPLE_SIZE, library=None):
    """The library check of this computer against the stored faces. Read-only."""
    from face_detect.jobs.automatic_people_scan import decode_canonical
    manifest, faces, paths, file_sha256 = (library or read_library)(cfg, data_root, size)
    load = library_check.verified(lambda path: decode_canonical(path)[0], paths, file_sha256)
    return library_check.check(service, manifest, faces, load, size=size)


def record_certificate(cfg, result):
    """Store a check's certificate in the catalog. Only a passed one lets this computer write."""
    from app import database
    from face_detect import catalog
    from face_detect.catalog.certify import record
    with database.connection(catalog.database(cfg)) as db:
        record(db, result)


def main(argv=None, service_factory=None, library=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--profile')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('describe')
    own = commands.add_parser('check-library', help='Certify this backend against a sample of the stored faces')
    own.add_argument('--sample', type=int, default=library_check.SAMPLE_SIZE)
    own.add_argument('--out', type=Path)
    own.add_argument('--record', action='store_true',
                     help="Store the certificate in this library's People catalog")
    carried = own.add_mutually_exclusive_group()
    carried.add_argument('--export-sample', type=Path, metavar='NEW_DIR',
                         help='Write the sample (faces, vectors, photo copies) to a new folder for another computer')
    carried.add_argument('--sample-from', type=Path, metavar='DIR', help='Check against a sample another computer wrote')
    make = commands.add_parser('make')
    make.add_argument('--images', type=Path, required=True)
    make.add_argument('--out', type=Path, required=True)
    make.add_argument('--source', required=True)
    check = commands.add_parser('check')
    check.add_argument('--golden', type=Path, required=True)
    check.add_argument('--tolerance', type=float, default=conformance.DEFAULT_TOLERANCE)
    check.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    if getattr(args, 'out', None) is not None and args.command != 'make' and args.out.exists():
        parser.error('--out must be a new file')
    if args.command == 'check-library' and not library_check.MIN_SAMPLES <= args.sample <= 200:
        parser.error(f'--sample must be {library_check.MIN_SAMPLES} to 200')
    if args.command == 'check-library' and args.export_sample is not None:
        # Reads the catalog and copies photos; the model isn't needed.
        if args.out is not None:
            parser.error('--export-sample writes a sample, not a certificate')
        if args.export_sample.exists():
            parser.error('--export-sample must be a new folder')
        reader = library or read_library
        manifest, faces, paths, file_sha256 = reader(load_config() if library is None else None, DATA, args.sample)
        print(json.dumps(library_check.write_sample(args.export_sample, manifest, faces, paths, file_sha256), indent=2))
        return 0
    cfg = None
    if service_factory is None:
        cfg = load_config()
        if args.profile:
            cfg['face_detection']['active_profile'] = args.profile
        service = create_service(cfg)
    else:
        service = service_factory()
    try:
        service.observe()
        if args.command == 'describe':
            result = service.describe()
        elif args.command == 'check-library':
            from face_detect.jobs.automatic_people_scan import decode_canonical
            if args.sample_from is not None:
                manifest, faces, paths, file_sha256 = library_check.read_sample(args.sample_from)
            elif library is None:
                manifest, faces, paths, file_sha256 = read_library(cfg if cfg is not None else load_config(), DATA, args.sample)
            else:
                manifest, faces, paths, file_sha256 = library(cfg, DATA, args.sample)
            load = library_check.verified(lambda path: decode_canonical(path)[0], paths, file_sha256)
            result = library_check.check(service, manifest, faces, load, size=args.sample)
        elif args.command == 'make':
            from face_detect.jobs.automatic_people_scan import decode_canonical
            paths = sorted(p for p in args.images.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
            result = conformance.make_golden_set(service, [decode_canonical(p)[0] for p in paths], args.out, args.source)
        else:
            if not 0 < args.tolerance <= 1:
                parser.error('--tolerance must be in (0, 1]')
            result = conformance.check(service, args.golden, tolerance=args.tolerance)
    finally:
        service.close()
    if args.command == 'check-library' and args.record:
        if args.sample_from is not None:
            parser.error('--record stores a check of this library, not of a carried sample')
        record_certificate(cfg if cfg is not None else load_config(), result)
    if args.command in ('check', 'check-library') and args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if args.command not in ('check', 'check-library') or result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
