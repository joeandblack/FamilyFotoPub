"""Rebuild an omitted face cache in memory, using recovered originals only."""
from io import BytesIO
import hashlib
import math

from fastapi import HTTPException
from fastapi.responses import Response


def crop_thumbnail(path, expected_hash, box):
    from face_detect.imaging.face_decode import decode_primary, MAX_BYTES

    values = [box[key] for key in ('x', 'y', 'width', 'height')]
    if any(type(value) not in (float, int) or not math.isfinite(value) for value in values):
        raise ValueError('Invalid saved face bounds')
    x, y, width, height = values
    if min(x, y) < 0 or min(width, height) <= 0 or x + width > 1.001 or y + height > 1.001:
        raise ValueError('Invalid saved face bounds')
    # Candidates are offline and validated; still check content before decoding a face.
    with path.open('rb') as source:
        digest = hashlib.sha256()
        total = 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError('Image exceeds preview byte limit')
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise ValueError('Recovered image checksum changed')
    image, _, _ = decode_primary(path)
    try:
        x, y, width, height = x * image.width, y * image.height, width * image.width, height * image.height
        pad = max(width, height) * .15
        bounds = (max(0, int(x - pad)), max(0, int(y - pad)),
                  min(image.width, int(x + width + pad)), min(image.height, int(y + height + pad)))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise ValueError('Saved face is too small to render')
        with image.crop(bounds) as crop:
            crop.thumbnail((160, 160))
            output = BytesIO()
            crop.save(output, format='JPEG', quality=85)
            return output.getvalue()
    finally:
        image.close()


def recovered_thumbnail(cfg, connect, did, kind):
    from app.library.media import safe_media_path
    from face_detect.api.readonly_people import thumbnail_query, live_photos

    query = thumbnail_query(kind).replace('SELECT d.id ', 'SELECT d.box,a.media_id,m.sha256 ', 1)
    with connect() as db:
        row = db.execute(query, (did,)).fetchone()
    if not row:
        raise HTTPException(404, 'Face unavailable')
    photos = live_photos(cfg, {row['media_id']: row['sha256']})
    if not photos:
        raise HTTPException(404, 'Photo unavailable')
    try:
        path = safe_media_path(cfg['destination']['root'], photos[0]['relative_path'])
        payload = crop_thumbnail(path, row['sha256'], row['box'])
    except (OSError, ValueError, KeyError):
        raise HTTPException(404, 'Recovered face thumbnail unavailable') from None
    return Response(payload, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})
