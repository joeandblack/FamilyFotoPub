"""Conservative JPEG fingerprints; no originals are changed."""
import hashlib
import io
import os
import json
import re
import subprocess
import sys
from PIL import Image, ImageOps, UnidentifiedImageError, __version__ as pillow_version

VERSION = 'jpeg-rgb-icc-exif-v1-pillow-' + pillow_version
MAX_BYTES = 80 * 1024 * 1024
MAX_PIXELS = 60_000_000
REVIEW_MAX_PIXELS = 60_000_000
UNRECOGNIZED_IMAGE = 'Image format could not be recognized; duplicate comparison skipped (file retained).'
WEBSITE_METADATA = 'Not an image: website metadata text (.jpg filename); duplicate comparison skipped (file retained).'


def readable_warning(message):
    if message and message.startswith('cannot identify image file'):
        return UNRECOGNIZED_IMAGE
    return message


def available_memory():
    import os
    if os.name == 'nt':
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
        value = Memory();value.length = ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            raise ValueError('Available memory could not be checked')
        return value.available
    if sys.platform == 'darwin':
        return _darwin_available_memory()
    try:
        return os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (ValueError, OSError) as error:
        raise ValueError('Available memory could not be checked') from error


def _darwin_counter(name):
    import ctypes
    libc = ctypes.CDLL('/usr/lib/libc.dylib', use_errno=True)
    value, size = ctypes.c_uint64(), ctypes.c_size_t(8)
    if libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0) or size.value not in (4, 8):
        raise ValueError('Available memory could not be checked')
    return value.value & 0xFFFFFFFF if size.value == 4 else value.value


def _darwin_available_memory():
    """Free, purgeable and file-cache pages: memory the kernel can hand back without swapping.

    Speculative pages are deliberately not added: XNU counts them in
    vm.page_pageable_external_count as well, so adding both would count them twice.
    """
    pages = sum(_darwin_counter(name) for name in ('vm.page_free_count', 'vm.page_purgeable_count',
                                                   'vm.page_pageable_external_count'))
    return pages * os.sysconf('SC_PAGE_SIZE')


def evidence_text(value, limit):
    # EXIF strings can contain NUL padding, which PostgreSQL JSONB rejects.
    text = str(value).replace('\x00', '')
    return text.encode('utf-8', errors='replace').decode('utf-8')[:limit]


def jpeg_image_data(data):
    """Remove only pre-scan EXIF and comments; retain all other JPEG bytes."""
    if data[:2] != b'\xff\xd8':
        raise ValueError('Not a JPEG')
    result = bytearray(data[:2])
    offset = 2
    while offset < len(data):
        start = offset
        if data[offset] != 255:
            raise ValueError('Malformed JPEG marker')
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            raise ValueError('Truncated JPEG')
        marker = data[offset]
        offset += 1
        if marker == 0xda:
            result.extend(data[start:])
            return bytes(result)
        if marker in (0xd8, 0xd9, 0x00) or 0xd0 <= marker <= 0xd7:
            raise ValueError('Unexpected JPEG marker')
        length = int.from_bytes(data[offset:offset+2], 'big')
        end = offset + length
        if length < 2 or end > len(data):
            raise ValueError('Truncated JPEG segment')
        exif = marker == 0xe1 and data[offset+2:offset+8] == b'Exif\0\0'
        if not exif and marker != 0xfe:
            result.extend(data[start:end])
        offset = end
    raise ValueError('JPEG has no scan')


def fingerprint(data, *, review=False, large_review=False):
    if large_review and not review:
        raise ValueError('Large-image scan requires manual review')
    try:
        return _fingerprint(data, review=review, large_review=large_review)
    except OSError as error:
        recoverable = (str(error) == 'broken data stream when reading image file' or
                       re.fullmatch(r'image file is truncated(?: \(\d+ bytes not processed\))?',str(error)) is not None)
        if not review or not recoverable or large_review:
            raise
        # Pillow's tolerant switch is global: contain it in a short-lived process.
        from ..config import ROOT
        try:
            child=subprocess.run([sys.executable,'-m','app.photos.recovery'],input=bytes(data),
                capture_output=True,timeout=120,cwd=ROOT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired as timeout:
            raise ValueError('Review-only image recovery timed out; original retained') from timeout
        if child.returncode:
            raise ValueError('Review-only image recovery failed; original retained')
        result=json.loads(child.stdout)
        result['evidence'].update(review_only=True,decode_mode='tolerant-jpeg',
            original_decode_error=str(error),
            recovery_warning='Damaged JPEG decoded tolerantly; pixels may be missing or reconstructed. Manual review required.')
        return result


def _fingerprint(data, *, review=False, large_review=False):
    if len(data) > MAX_BYTES:
        raise ValueError('JPEG exceeds 80 MB scan limit')
    try:
        source = Image.open(io.BytesIO(data))
    except UnidentifiedImageError as error:
        metadata = data.startswith(b'vti_encoding:') and b'\nvti_extenderversion:' in data[:4096]
        raise ValueError(WEBSITE_METADATA if metadata else UNRECOGNIZED_IMAGE) from error
    with source:
        if not review and (source.format != 'JPEG' or source.width * source.height > MAX_PIXELS):
            raise ValueError('Only JPEG images up to 60 megapixels are scanned')
        if not review and source.width * source.height > 40_000_000:
            if source.width * source.height * 16 + len(data)*2 + 64*1024*1024 > available_memory()/2:
                raise ValueError('Insufficient available memory for full-resolution duplicate scan')
        if review:
            if source.format not in ('JPEG', 'PNG', 'WEBP', 'BMP', 'TIFF', 'MPO'):
                raise ValueError('Actual image format is not supported by the review scan: '+str(source.format))
            pixels = source.width * source.height
            if pixels > REVIEW_MAX_PIXELS and not large_review:
                raise ValueError('Image exceeds manual review limit of 60 megapixels')
            if pixels*16 + len(data)*2 + 64*1024*1024 > available_memory()/2:
                raise ValueError('Insufficient available memory for full-resolution review scan')
            if 'A' in source.getbands() or 'transparency' in source.info:
                raise ValueError('Transparent images require a separate comparison method')
        frame_count = getattr(source, 'n_frames', 1)
        mpo_primary = review and source.format == 'MPO'
        if frame_count != 1 and not mpo_primary:
            raise ValueError('Multi-frame JPEG is excluded')
        # MPO opens on its primary image. Other frames are not evidence of a match.
        if mpo_primary:source.seek(0)
        exif = source.getexif()
        orientation = exif.get(274, 1)
        original_orientation = orientation
        if orientation not in range(1, 9):
            if not review:raise ValueError('Invalid EXIF orientation')
            orientation = 1
            exif[274] = 1
        icc = hashlib.sha256(source.info.get('icc_profile', b'')).hexdigest()
        details = exif.get_ifd(34665) if 34665 in exif else {}
        color = [details.get(40961), str(details.get(42240, ''))]
        # Different ICC profiles never match in v1, even if RGB samples agree.
        with ImageOps.exif_transpose(source) as oriented:
            with oriented.convert('RGB') as rgb:
                header = json.dumps([VERSION, rgb.size, icc, source.mode, color]).encode()
                pixels = hashlib.sha256(header)
                for y in range(0, rgb.height, 128):
                    with rgb.crop((0, y, rgb.width, min(y+128, rgb.height))) as stripe:
                        pixels.update(stripe.tobytes())
        evidence = {'width': source.width, 'height': source.height,
                    'orientation': orientation, 'icc_sha256': icc,
                    'exif_sha256': hashlib.sha256(source.info.get('exif', b'')).hexdigest(),
                    'has_gps': 34853 in exif,
                    'gps': evidence_text(exif.get_ifd(34853), 500) if 34853 in exif else '',
                    'camera': evidence_text(exif.get(272, ''), 200),
                    'date': evidence_text(details.get(36867, exif.get(306, '')), 100)}
        if review:evidence.update(review_only=True, actual_format=source.format, original_orientation=evidence_text(original_orientation,100), review_policy='full-resolution-v1-60mp')
        if large_review:evidence.update(review_policy='explicit-large-image-v1',large_image_review=True)
        if mpo_primary:evidence.update(comparison_scope='primary-image-only', frame_count=frame_count, compared_frame=0, additional_frames_compared=False)
        image_hash = hashlib.sha256(str(orientation).encode() + (jpeg_image_data(data) if source.format=='JPEG' else data)).hexdigest()
        return dict(version=VERSION, pixel_hash=pixels.hexdigest(), image_hash=image_hash, evidence=evidence)
