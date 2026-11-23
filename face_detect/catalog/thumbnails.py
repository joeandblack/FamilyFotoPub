"""Face thumbnails: a cache in data/people/thumbnails, rebuilt from the photos when missing.

Backups leave the face thumbnails out (they're rebuilt from the photos), so a library
restored on another computer has none. `rebuild` makes them all, decoding each photo once
for all its faces; it skips thumbnails already there, so it can be stopped and run again.
`on_demand` makes one when the People page asks for a missing one, and keeps it.

Photos are only read, and each is checked against its checksum first. A file name is
tried as stored, then in its NFC and NFD spellings, as a restored exFAT disk may hold it.
"""
from io import BytesIO
import hashlib
import math
from pathlib import Path

FOLDER = 'people/thumbnails'
SIZE = 160
PAD = .15

FACES = '''SELECT d.id, d.box, m.relative_path, coalesce(m.stored_sha256, m.sha256) file_sha256
    FROM face_detections d JOIN face_analyses a ON a.id = d.analysis_id
    JOIN media m ON m.id = a.media_id AND m.sha256 = a.content_hash
    WHERE m.kind = 'image'
      AND NOT EXISTS (SELECT 1 FROM media_trash t WHERE t.media_id = m.id AND t.deleted_at IS NOT NULL)'''


def folder(data_root):
    return Path(data_root) / FOLDER


def photo(root, relative):
    """The photo as this disk spells its name, never outside the library root."""
    import unicodedata
    root = Path(root).resolve()
    for form in (None, 'NFC', 'NFD'):
        candidate = (root / (relative if form is None else unicodedata.normalize(form, relative))).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError('Photo path outside the library')
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('Photo not found')


def _box(box):
    values = [box.get(k) for k in ('x', 'y', 'width', 'height')] if isinstance(box, dict) else []
    if len(values) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('Invalid saved face bounds')
    x, y, width, height = values
    if min(x, y) < 0 or min(width, height) <= 0 or x + width > 1.001 or y + height > 1.001:
        raise ValueError('Invalid saved face bounds')
    return values


def render(path, file_sha256, boxes):
    """{face id: JPEG bytes} for faces in one photo, decoding it once. Read-only."""
    from face_detect.imaging.face_decode import MAX_BYTES, decode_primary
    digest = hashlib.sha256()
    total = 0
    with Path(path).open('rb') as source:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError('Image exceeds the decode byte limit')
            digest.update(chunk)
    if digest.hexdigest() != file_sha256:
        raise ValueError('Photo checksum changed')
    # As the scan worker decodes (decode_canonical), so the crop and its rounding match.
    image, _, _ = decode_primary(path, max_long_edge=4096)
    result = {}
    try:
        for did, box in boxes.items():
            x, y, width, height = _box(box)
            x, y, width, height = x * image.width, y * image.height, width * image.width, height * image.height
            pad = max(width, height) * PAD
            bounds = (max(0, int(x - pad)), max(0, int(y - pad)),
                      min(image.width, int(x + width + pad)), min(image.height, int(y + height + pad)))
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue
            with image.crop(bounds) as crop:
                crop.thumbnail((SIZE, SIZE))
                output = BytesIO()
                crop.convert('RGB').save(output, format='JPEG', quality=85)
                result[did] = output.getvalue()
    finally:
        image.close()
    return result


def _write(target, did, data):
    """Publish one thumbnail. Each writer has its own temporary name, so two requests for the
    same face (or the rebuild and a page) can't trip over each other; the last one wins."""
    import os
    import uuid
    target.mkdir(parents=True, exist_ok=True)
    path = target / f'{int(did)}.jpg'
    temporary = target / f'.{int(did)}.{uuid.uuid4().hex}.tmp'
    try:
        temporary.write_bytes(data)
        try:
            os.replace(temporary, path)
        except PermissionError:
            # Windows refuses a rename onto a file another writer is renaming at that moment.
            # Both hold the same face, so if theirs is published, ours is not needed.
            if not path.is_file():
                raise
    finally:
        temporary.unlink(missing_ok=True)
    return path


def rebuild(connect, media_root, data_root, limit=None, progress=None):
    """Make every missing face thumbnail. Returns counts. Reads the catalog read-only."""
    target = folder(data_root)
    present = {p.stem for p in target.glob('*.jpg')} if target.is_dir() else set()
    with connect() as db:
        db.execute('SET TRANSACTION READ ONLY')
        rows = db.execute(FACES + ' ORDER BY m.relative_path, d.id').fetchall()
    by_photo = {}
    for row in rows:
        if str(row['id']) in present:
            continue
        by_photo.setdefault((row['relative_path'], row['file_sha256']), {})[row['id']] = row['box']
    counts = {'faces': len(rows), 'already_present': len(rows) - sum(map(len, by_photo.values())),
              'rebuilt': 0, 'photos': 0, 'failed_photos': {}}
    for n, ((relative, sha), boxes) in enumerate(by_photo.items()):
        if limit is not None and counts['photos'] >= limit:
            break
        counts['photos'] += 1
        try:
            for did, data in render(photo(media_root, relative), sha, boxes).items():
                _write(target, did, data)
                counts['rebuilt'] += 1
        except (OSError, ValueError) as error:
            reason = type(error).__name__ if isinstance(error, OSError) else str(error)
            counts['failed_photos'][reason] = counts['failed_photos'].get(reason, 0) + 1
        if progress and n % 200 == 0:
            progress(counts)
    counts['remaining_photos'] = len(by_photo) - counts['photos']
    return counts


def on_demand(connect, media_root, data_root, did):
    """Make one missing face thumbnail and keep it. Returns its path, or None if it can't be made."""
    with connect() as db:
        db.execute('SET TRANSACTION READ ONLY')
        row = db.execute(FACES + ' AND d.id = %s', (did,)).fetchone()
    if not row:
        return None
    try:
        data = render(photo(media_root, row['relative_path']), row['file_sha256'], {row['id']: row['box']}).get(row['id'])
        return _write(folder(data_root), row['id'], data) if data else None
    except (OSError, ValueError):
        return None
