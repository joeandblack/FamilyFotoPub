"""Durable, readable per-photo warnings without decoding media in HTTP requests."""
import json
import re
from PIL import Image, ImageCms


def decode_detail(error):
    if isinstance(error, ImageCms.PyCMSError):
        return 'The embedded color profile could not be converted to sRGB for face detection.'
    if isinstance(error, Image.DecompressionBombError):
        return 'The image exceeds the decoder safety limit for pixel count.'
    message = str(error)
    # Only expose known decoder messages, never arbitrary OSError paths or addresses.
    if isinstance(error, ValueError) and message.startswith((
            'Image exceeds ', 'Image color profile exceeds ', 'Ambiguous MPO ',
            'MPO decoder must ', 'Multiframe images ', 'Image format ',
            'Unrecognized image data')):
        return message
    if 'image file is truncated' in message.lower():
        return 'The image data is incomplete (truncated); face detection could not decode it safely.'
    if 'broken data stream' in message.lower():
        return 'The image contains a broken data stream and could not be decoded for face detection.'
    return 'The image could not be decoded safely for face detection.'


def import_warnings(db, root, job_id):
    """Read one import checkpoint and resolve warning names in one catalog query."""
    path = root / 'jobs' / f'import-{int(job_id)}' / 'import.json'
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    items = [item for item in state.get('items', []) if item.get('error')]
    if not items:
        return []
    names = {row['id']: row['name'] for row in db.execute(
        'SELECT id,name FROM media WHERE id=ANY(%s)',
        ([item['media_id'] for item in items],)).fetchall()}
    result = []
    for item in items:
        detail = item.get('error_detail')
        # A running worker loaded before this change may only copy the error code.
        child = item.get('child', '')
        if not detail and re.fullmatch(r'[a-z0-9-]{1,64}', child):
            try:
                raw = json.loads((root / 'jobs' / child / 'raw.json').read_text())
                detail = next((i.get('error_detail') for i in raw.get('items', [])
                               if i.get('media_id') == item['media_id']), None)
            except FileNotFoundError:
                pass
        result.append(dict(stage='Discover people', severity='warning',
                           media_id=item['media_id'], name=names.get(item['media_id']),
                           error=item['error'], detail=detail))
    return result
