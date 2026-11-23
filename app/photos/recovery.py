"""Isolated, bounded tolerant JPEG decoding for explicitly requested review."""
import io
import json
import sys
from PIL import Image, ImageFile
from .fingerprint import MAX_BYTES, _fingerprint


def main():
    data=sys.stdin.buffer.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES:
        raise ValueError('Image exceeds scan byte limit')
    with Image.open(io.BytesIO(data)) as image:
        if image.format!='JPEG':
            raise ValueError('Tolerant recovery is limited to JPEG')
    ImageFile.LOAD_TRUNCATED_IMAGES=True
    result=_fingerprint(data,review=True)
    sys.stdout.write(json.dumps(result))


if __name__=='__main__':
    main()
