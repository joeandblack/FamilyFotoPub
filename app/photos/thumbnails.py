"""Hash-sharded thumbnails with legacy flat-layout reads during migration."""
import hashlib
from pathlib import Path
import re
import stat
import os

from PIL import Image


def thumbnail_path(root, sha):
    if not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise ValueError('Invalid thumbnail hash')
    return Path(root) / sha[:2] / sha[2:4] / f'{sha}.jpg'


def find_thumbnail(root, sha):
    sharded = thumbnail_path(root, sha)
    if sharded.is_file():
        return sharded
    return Path(root) / f'{sha}.jpg'


def cache_files(data, sha):
    """Exact content-owned paths only; reject redirected directories/files."""
    thumbnail_path(data, sha)  # Validate the entire key before path construction.
    base = Path(data).resolve()
    paths = []
    def checked(path):
        for part in reversed((path, *path.parents)):
            if part == base or not part.is_relative_to(base):
                continue
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Cache path is redirected; deletion stopped')
        if not path.resolve().is_relative_to(base):
            raise ValueError('Cache path outside data directory')
        return path
    for directory in ('thumbnails', 'previews'):
        root = base / directory
        for target in (root / f'{sha}.jpg', thumbnail_path(root, sha)):
            checked(target)
            paths.append(target)
            # New HEIF partials are attributable; old random partials cannot
            # safely be assigned to any one photo and are never swept here.
            try:
                with os.scandir(target.parent) as entries:
                    for entry in entries:
                        if entry.name.startswith(target.name + '.') and entry.name.endswith('.partial'):
                            paths.append(checked(Path(entry.path)))
            except FileNotFoundError:
                pass  # Permission and other enumeration failures must propagate.
            paths.append(checked(target.with_name(sha + '.video-partial.jpg')))
    return paths


def remove_cached_media(data, sha):
    # Caller holds the content lock until the deletion transaction completes.
    for path in cache_files(data, sha):
        path.unlink(missing_ok=True)


def migrate_one(root, sha):
    target = thumbnail_path(root, sha)
    flat = Path(root) / f'{sha}.jpg'
    source = flat if flat.is_file() else target
    if not source.is_file():
        raise FileNotFoundError(f'Thumbnail missing: {sha}')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with Image.open(source) as image:
        image.verify()
    if flat.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != before:
                raise OSError(f'Conflicting thumbnail; neither file changed: {sha}')
            flat.unlink()
        else:
            # Same-volume rename is atomic; a crash leaves either layout readable.
            flat.rename(target)
    if hashlib.sha256(target.read_bytes()).hexdigest() != before:
        raise OSError(f'Thumbnail verification failed: {sha}')
    return target


def migrate():
    from ..config import DATA
    from ..database import connection
    from ..imports.importer import IMPORT_LOCK
    from ..telemetry import utcnow, write_json
    from ..library.locks import lock_content, unlock_content
    import json
    review = json.loads((DATA/'post-import-review.json').read_text())
    if review['state'] != 'passed':
        raise RuntimeError('Completed integrity and browser review required')
    report = {'started': utcnow(), 'state': 'running', 'checked': 0}
    with connection() as db:
        if not db.execute('SELECT pg_try_advisory_lock(%s) locked', (IMPORT_LOCK,)).fetchone()['locked']:
            raise RuntimeError('Importer active; migration deferred')
        if db.execute("SELECT count(*) count FROM jobs WHERE state NOT IN ('completed-review-required','cancelled')").fetchone()['count']:
            raise RuntimeError('All import batches must be reviewed first')
        rows = db.execute("SELECT sha256 FROM media WHERE kind='image' AND metadata_error IS NULL ORDER BY id").fetchall()
        db.commit()
        report['total'] = len(rows)
        try:
            for row in rows:
                key = lock_content(db, row['sha256'])
                try:
                    if db.execute('SELECT 1 FROM media m JOIN media_trash t ON t.media_id=m.id WHERE m.sha256=%s AND t.deleting_at IS NOT NULL', (row['sha256'],)).fetchone():
                        continue
                    migrate_one(DATA/'thumbnails', row['sha256'])
                finally:
                    unlock_content(db, key)
                    db.commit()
                report['checked'] += 1
                if report['checked'] % 500 == 0:
                    write_json(DATA/'thumbnail-migration.json', report)
                    print(f"Verified {report['checked']} / {len(rows)} thumbnails", flush=True)
            report['state'] = 'completed'
        except Exception as error:
            report['state'] = 'failed'
            report['error'] = str(error)
            raise
        finally:
            report['finished'] = utcnow()
            write_json(DATA/'thumbnail-migration.json', report)
    print(report, flush=True)


if __name__ == '__main__':
    migrate()
