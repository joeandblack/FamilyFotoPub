"""Bounded HEIF decoding; originals are only opened for reading."""
from datetime import datetime
from io import BytesIO
from pathlib import Path
import os
import tempfile
from PIL import Image, ImageCms, ImageOps
from pillow_heif import register_heif_opener

# Registered in every API/importer/repair process through app.library.media.
# Keep libheif security limits enabled and avoid decoding auxiliary/depth images.
register_heif_opener(thumbnails=False, depth_images=False, aux_images=False, decode_threads=2)
MAX_PIXELS = 80_000_000
MAX_BYTES = 128 * 1024 * 1024


def is_heif(path):
    return Path(path).suffix.lower() in ('.heic', '.heif')


def atomic_jpeg(image, target, quality, icc=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        # Keep staging names short: recovery targets already contain a hash and UUID.
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='ff-', suffix='.partial', delete=False) as stream:
            name = stream.name
            image.save(stream, 'JPEG', quality=quality, icc_profile=icc)
            stream.flush()
            os.fsync(stream.fileno())
        with Image.open(name) as check:
            check.verify()
        os.replace(name, target)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def create_derivatives(source, thumbnail, preview=None):
    if Path(source).stat().st_size > MAX_BYTES:
        raise ValueError('HEIF exceeds 128 MiB decode limit; original retained')
    with Image.open(source) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('HEIF exceeds 80 megapixel decode limit; original retained')
        exif = image.getexif()
        nested = exif.get_ifd(34665) if 34665 in exif else {}
        raw = nested.get(36867) or nested.get(36868) or exif.get(306)
        captured = None
        if raw:
            try:
                captured = datetime.strptime(str(raw).strip('\x00 '), '%Y:%m:%d %H:%M:%S')
            except ValueError:
                pass
        # libheif applies container rotation/mirroring; its plugin normalizes
        # EXIF orientation. exif_transpose handles any remaining EXIF transform.
        display = ImageOps.exif_transpose(image)
        width, height = display.size
        profile = image.info.get('icc_profile')
        rgb = display if display.mode == 'RGB' else display.convert('RGB')
        icc = None
        if profile:
            if len(profile) > 4 * 1024 * 1024:
                raise ValueError('HEIF color profile exceeds 4 MiB limit')
            srgb = ImageCms.createProfile('sRGB')
            rgb = ImageCms.profileToProfile(rgb, ImageCms.ImageCmsProfile(BytesIO(profile)), srgb, outputMode='RGB')
            icc = ImageCms.ImageCmsProfile(srgb).tobytes()
        # Derivatives are SDR JPEG; no EXIF/XMP is copied into caches. Original
        # HDR data, profiles, EXIF and provenance remain in the unchanged HEIF.
        if preview:
            large = ImageOps.contain(rgb, (min(rgb.width, 2560), min(rgb.height, 2560)), Image.Resampling.LANCZOS)
            atomic_jpeg(large, preview, 90, icc)
        rgb.thumbnail((640, 480), Image.Resampling.LANCZOS)
        atomic_jpeg(rgb, thumbnail, 82, icc)
        return captured, width, height


def guarded_derivatives(request):
    """Repair child retains its own lease even if the parent process exits."""
    import psycopg
    from ..library.locks import MEDIA_LOCK_NAMESPACE, content_key
    with psycopg.connect(**request['database']) as db:
        db.execute('SELECT pg_advisory_lock_shared(%s,%s)',
                   (MEDIA_LOCK_NAMESPACE, content_key(request['sha'])))
        if not db.execute('SELECT 1 FROM pg_stat_activity WHERE pid=%s', (request['owner'],)).fetchone():
            raise RuntimeError('Repair owner exited; derivatives cancelled')
        if not db.execute('SELECT 1 FROM media m WHERE id=%s AND sha256=%s AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)',
                          (request['media_id'], request['sha'])).fetchone():
            raise RuntimeError('Photo is no longer eligible for repair')
        return create_derivatives(request['source'], request['thumbnail'], request['preview'])


if __name__ == '__main__':
    from ..backup.admission import hold_process_lease
    from ..config import DATA
    hold_process_lease(DATA)
    import json
    import sys
    captured, width, height = (guarded_derivatives(json.load(sys.stdin)) if sys.argv[1:] == ['--guarded']
                               else create_derivatives(*sys.argv[1:]))
    print(json.dumps({'captured_at': captured.isoformat() if captured else None, 'width': width, 'height': height}))
