import hashlib
from types import SimpleNamespace

import pytest
from PIL import Image

from face_detect.imaging.face_decode import decode_primary
from face_detect.imaging.face_decode import validate_primary


def test_primary_mpo_is_oriented_and_source_unchanged(tmp_path):
    source = tmp_path / 'ordinary.jpg'
    exif = Image.Exif()
    exif[274] = 6
    Image.new('RGB', (80, 40), 'red').save(
        source, format='MPO', save_all=True,
        append_images=[Image.new('RGB', (8, 4), 'blue')], exif=exif)
    before = hashlib.sha256(source.read_bytes()).digest()
    rgb, width, height = decode_primary(source, 20)
    assert (width, height) == (40, 80)
    assert rgb.size == (10, 20)
    assert rgb.getpixel((5, 10))[0] > 240
    assert hashlib.sha256(source.read_bytes()).digest() == before


@pytest.mark.parametrize('format,extension', [('GIF', 'gif'), ('TIFF', 'tiff'), ('PNG', 'png')])
def test_ambiguous_multiframe_rejected(tmp_path, format, extension):
    source = tmp_path / ('animated.' + extension)
    Image.new('RGB', (8, 8), 'red').save(source, format=format, save_all=True,
        append_images=[Image.new('RGB', (8, 8), 'blue')])
    with pytest.raises(ValueError, match='Multiframe'):
        decode_primary(source)


def fake_mpo():
    attr = dict(MPType='Baseline MP Primary Image', ImageDataFormat='JPEG',
                DependentChildImageFlag=False, RepresentativeImageFlag=False)
    return SimpleNamespace(width=10, height=10, format='MPO', n_frames=2,
        mpinfo={0xB002: [dict(Attribute=attr, Size=50, DataOffset=0),
            dict(Attribute={**attr, 'MPType': 'Undefined'}, Size=20, DataOffset=40)]}, tell=lambda: 0)


@pytest.mark.parametrize('change', ['duplicate', 'missing', 'other-representative', 'child', 'count', 'oversize', 'offset', 'not-jpeg', 'not-first'])
def test_mpo_ambiguity_rejected(change):
    image = fake_mpo()
    entries = image.mpinfo[0xB002]
    if change == 'duplicate': entries[1]['Attribute']['MPType'] = 'Baseline MP Primary Image'
    if change == 'missing': entries[0]['Attribute']['MPType'] = 'Undefined'
    if change == 'other-representative': entries[1]['Attribute']['RepresentativeImageFlag'] = True
    if change == 'child': entries[0]['Attribute']['DependentChildImageFlag'] = True
    if change == 'count': image.n_frames = 99
    if change == 'oversize': entries[0]['Size'] = 101
    if change == 'offset': entries[0]['DataOffset'] = 1
    if change == 'not-jpeg': entries[0]['Attribute']['ImageDataFormat'] = 'PNG'
    if change == 'not-first': image.tell = lambda: 1
    with pytest.raises(ValueError, match='MPO'):
        validate_primary(image, 100)


def test_pixel_budget_and_resolution_guard():
    image = fake_mpo()
    image.width = 80_000_001
    with pytest.raises(ValueError, match='megapixel'):
        validate_primary(image, 100)
    with pytest.raises(ValueError, match='resolution'):
        decode_primary('unused', 10000)
