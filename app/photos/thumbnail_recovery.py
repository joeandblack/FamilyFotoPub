"""Recover display thumbnails without relaxing original-image validation."""
import base64
from datetime import datetime
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid
from PIL import Image, ImageFile, ImageOps
from ..config import ROOT
from .heif import atomic_jpeg
from ..library.media import image_metadata, digest, safe_media_path, TrafficLimit
from .thumbnails import find_thumbnail, thumbnail_path

MAX_BYTES = 80 * 1024 * 1024


def create_thumbnail(source, thumbnail, preview=None):
    try:
        return (*image_metadata(source, thumbnail, preview), None)
    except OSError as error:
        if not (str(error) == 'broken data stream when reading image file' or
                re.fullmatch(r'image file is truncated(?: \(\d+ bytes not processed\))?', str(error))):
            raise
        decode_error = str(error)
    with Path(source).open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Thumbnail recovery exceeds 80 MiB limit; original retained')
    # Pillow's tolerant flag must never affect normal scans or other threads.
    try:
        child = subprocess.run([sys.executable, '-m', 'app.photos.thumbnail_recovery'],
            input=data, capture_output=True, timeout=120, cwd=ROOT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as error:
        raise ValueError('Thumbnail recovery timed out; original retained') from error
    if child.returncode:
        raise ValueError('Thumbnail recovery failed; original retained')
    result = json.loads(child.stdout)
    warning = f"Thumbnail recovered from damaged {result['format']}; visual defects are possible. Original decode error: {decode_error}"
    with Image.open(io.BytesIO(base64.b64decode(result['thumbnail'], validate=True))) as image:
        if image.format != 'JPEG' or image.width > 640 or image.height > 480:
            raise ValueError('Invalid recovered thumbnail')
        atomic_jpeg(image, thumbnail, 82)
    captured = datetime.fromisoformat(result['captured']) if result['captured'] else None
    return captured, result['width'], result['height'], warning


def rebuild_missing(db, row, config, data_root, guard=lambda: None):
    """Caller reserves maintenance; the content lock excludes deletion/edits."""
    from ..library.locks import lock_content, unlock_content
    key = lock_content(db, row['sha256'])
    stage = None
    try:
        if db.execute('SELECT 1 FROM media_trash WHERE media_id=%s AND deleting_at IS NOT NULL', (row['id'],)).fetchone():
            return 'excluded'
        if find_thumbnail(data_root/'thumbnails', row['sha256']).is_file():
            return 'exists'
        source = safe_media_path(config['destination']['root'], row['relative_path'])
        expected = row.get('stored_sha256') or row['sha256']
        before = source.stat()
        limit = TrafficLimit(config.get('io_mibps', 40))
        if before.st_size != row['size'] or digest(source, guard, limit) != expected:
            raise OSError('Original size/hash differs from catalog; thumbnail repair refused')
        target = thumbnail_path(data_root/'thumbnails', row['sha256'])
        stage = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
        captured, width, height, warning = create_thumbnail(source, stage,
            thumbnail_path(data_root/'previews', row['sha256']) if source.suffix.lower() in ('.heic', '.heif') else None)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or digest(source, guard, limit) != expected:
            raise OSError('Original changed during thumbnail repair; repair refused')
        guard()
        stage.replace(target)
        db.execute('UPDATE media SET captured_at=coalesce(captured_at,%s),width=coalesce(width,%s),height=coalesce(height,%s),metadata_error=%s WHERE id=%s',
            (captured, width, height, warning, row['id']))
        return 'recovered' if warning else 'rebuilt'
    finally:
        if stage is not None:stage.unlink(missing_ok=True)
        unlock_content(db, key)


def main():
    from .fingerprint import available_memory
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:raise ValueError('Image byte limit exceeded')
    with Image.open(io.BytesIO(data)) as image:
        pixels = image.width * image.height
        if image.format not in ('JPEG', 'PNG') or pixels > 60_000_000:
            raise ValueError('Tolerant thumbnail recovery requires JPEG or PNG up to 60 megapixels')
        if pixels * 16 + len(data) * 2 + 64*1024*1024 > available_memory()/2:
            raise ValueError('Insufficient memory for thumbnail recovery')
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        exif = image.getexif()
        nested = exif.get_ifd(34665) if 34665 in exif else {}
        raw = nested.get(36867) or nested.get(36868) or exif.get(306)
        captured = None
        if raw:
            try:captured = datetime.strptime(str(raw).strip('\x00 '), '%Y:%m:%d %H:%M:%S').isoformat()
            except ValueError:pass
        display = ImageOps.exif_transpose(image)
        display.thumbnail((640, 480))
        output = io.BytesIO()
        display.convert('RGB').save(output, 'JPEG', quality=82)
        print(json.dumps({'format': image.format, 'width': image.width, 'height': image.height, 'captured': captured,
                          'thumbnail': base64.b64encode(output.getvalue()).decode('ascii')}))


if __name__ == '__main__':main()
