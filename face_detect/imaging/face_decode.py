"""Bounded primary-image decoding for versioned face preprocessing.

MPO is a JPEG container, not necessarily animation. Only an explicitly marked,
unique first baseline primary is accepted; auxiliary images are never decoded.
Callers must still use the killable worker and verify the source checksum.
"""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

MAX_BYTES = 128 * 1024 * 1024
MAX_PIXELS = 80_000_000
MAX_MPO_ENTRIES = 32


def validate_primary(image, source_size):
    """Validate the primary selection without seeking to another image.

    Auxiliary offsets are never dereferenced: only the leading JPEG codestream
    is loaded. An undefined auxiliary MP type therefore does not require us to
    guess whether its contents represent a thumbnail, gain map, or another view.
    """
    if image.width * image.height > MAX_PIXELS:
        raise ValueError('Image exceeds 80 megapixel decode limit')
    frames = getattr(image, 'n_frames', 1)
    if image.format == 'MPO':
        entries = getattr(image, 'mpinfo', {}).get(0xB002, [])
        if not 1 <= frames <= MAX_MPO_ENTRIES or len(entries) != frames:
            raise ValueError('Ambiguous MPO primary image')
        attrs = [entry.get('Attribute', {}) for entry in entries]
        primary = [i for i, attr in enumerate(attrs)
                   if attr.get('MPType') == 'Baseline MP Primary Image']
        if (primary != [0] or entries[0].get('DataOffset') != 0
                or attrs[0].get('DependentChildImageFlag')
                or any(attr.get('RepresentativeImageFlag') for attr in attrs[1:])
                or any(attr.get('ImageDataFormat') != 'JPEG' for attr in attrs)
                or any(not isinstance(entry.get('Size'), int)
                       or not 0 < entry['Size'] <= source_size for entry in entries)):
            raise ValueError('Ambiguous MPO primary image')
        if image.tell() != 0:
            raise ValueError('MPO decoder must begin at the primary image')
    elif frames != 1:
        raise ValueError(f'Multiframe images without an explicit primary are not supported ({image.format}, {frames} frames)')
    elif image.format not in ('JPEG', 'PNG', 'HEIF', 'WEBP', 'BMP', 'TIFF'):
        raise ValueError(f'Image format {image.format} is not supported for face detection')


def decode_primary(source, max_long_edge=1600):
    """Return detached sRGB image and canonical full-resolution (width,height).

    EXIF orientation is applied to the primary before resizing. This does not
    flatten, rewrite, or otherwise alter the source, or inspect auxiliary pixels.
    """
    from app.photos import heif  # noqa: F401: register the existing HEIF decoder
    if not isinstance(max_long_edge, int) or not 1 <= max_long_edge <= 4096:
        raise ValueError('Invalid decode resolution')
    size = Path(source).stat().st_size
    if size > MAX_BYTES:
        raise ValueError('Image exceeds 128 MiB decode limit')
    try:
        with Image.open(source) as image:
            validate_primary(image, size)
            profile = image.info.get('icc_profile')
            if profile and len(profile) > 4 * 1024 * 1024:
                raise ValueError('Image color profile exceeds 4 MiB limit')
            canonical = ImageOps.exif_transpose(image)
            width, height = canonical.size
            rgb = canonical.convert('RGB')
            if profile:
                rgb = ImageCms.profileToProfile(
                    rgb, ImageCms.ImageCmsProfile(BytesIO(profile)),
                    ImageCms.createProfile('sRGB'), outputMode='RGB')
            rgb.thumbnail((max_long_edge, max_long_edge), Image.Resampling.LANCZOS)
            return rgb, width, height
    except UnidentifiedImageError as error:
        raise ValueError('Unrecognized image data') from error
