from datetime import datetime
from contextlib import nullcontext
import hashlib
import sys
from pathlib import Path
import time
from PIL import Image, ImageOps
from ..photos.heif import is_heif, create_derivatives

IMAGES={'.jpg','.jpeg','.png','.gif','.bmp','.tif','.tiff','.webp','.heic','.heif','.dng','.cr2','.nef'}
VIDEOS={'.mp4','.mov','.avi','.flv','.mkv','.m4v','.mts','.m2ts','.mpg','.mpeg','.3gp','.wmv','.vob'}
AUDIO={'.mp3','.m4a','.m4b','.aac','.wav','.flac','.ogg','.opus','.wma','.aif','.aiff'}
CHUNK=4*1024*1024

def kind_for(path):
    extension=Path(path).suffix.lower()
    # Audio shares the existing non-photo copy/metadata pipeline for now.
    return 'image' if extension in IMAGES else 'video' if extension in VIDEOS|AUDIO else None

class TrafficLimit:
    def __init__(self, mibps, timings=None):
        self.rate=mibps*1024*1024
        self.started=time.monotonic()
        self.bytes=0
        self.timings=timings

    def account(self, count):
        if not self.rate:
            return
        self.bytes+=count
        delay=self.bytes/self.rate-(time.monotonic()-self.started)
        if delay>0:
            with self.timings.measure('throttle_wait') if self.timings else nullcontext():
                time.sleep(delay)

def digest(path,guard,limit):
    result=hashlib.sha256()
    guard()
    with Path(path).open('rb') as stream:
        while True:
            guard()
            chunk=stream.read(CHUNK)
            if not chunk:
                break
            result.update(chunk)
            limit.account(len(chunk))
    return result.hexdigest()

def copy_verified(source, target, expected, guard, limit, timings=None):
    guard()
    target=Path(target)
    target.parent.mkdir(parents=True,exist_ok=True)
    partial=target.with_suffix(target.suffix+'.partial')
    with (timings.measure('copy_read_write') if timings else nullcontext()), Path(source).open('rb') as incoming,partial.open('wb') as outgoing:
        while True:
            guard()
            with timings.measure('copy_source_read') if timings else nullcontext():
                chunk=incoming.read(CHUNK)
            if not chunk:
                break
            with timings.measure('destination_write') if timings else nullcontext():
                outgoing.write(chunk)
            limit.account(len(chunk)*2)
        with timings.measure('destination_flush') if timings else nullcontext():
            outgoing.flush()
            import os
            os.fsync(outgoing.fileno())
    with timings.measure('destination_verify') if timings else nullcontext():
        if digest(partial,guard,limit)!=expected:
            raise OSError('Destination SHA-256 verification failed')
    guard()
    partial.replace(target)

def image_metadata(source, thumbnail, preview=None):
    if is_heif(source):
        return create_derivatives(source, thumbnail, preview)
    with Image.open(source) as image:
        exif=image.getexif()
        nested=exif.get_ifd(34665) if 34665 in exif else {}
        raw=nested.get(36867) or nested.get(36868) or exif.get(306)
        captured=None
        if raw:
            try:
                captured=datetime.strptime(str(raw).strip('\x00 '),'%Y:%m:%d %H:%M:%S')
            except ValueError:
                pass
        width,height=image.size
        preview=ImageOps.exif_transpose(image)
        preview.thumbnail((640,480))
        from ..photos.heif import atomic_jpeg
        atomic_jpeg(preview.convert('RGB'),thumbnail,82)
        return captured,width,height

def safe_media_path(root,relative):
    root=Path(root).resolve()
    path=(root/relative).resolve()
    if sys.platform == 'darwin':
        from ..storage.macos_disks import existing_parent
        if existing_parent(path).stat().st_dev != root.stat().st_dev:
            raise OSError('Media path crosses onto another mounted volume')
    if not path.is_relative_to(root):
        raise ValueError('Media path outside destination')
    return path
