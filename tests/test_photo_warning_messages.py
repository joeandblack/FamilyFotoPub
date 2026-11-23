import pytest
from app.photos.fingerprint import fingerprint, readable_warning, UNRECOGNIZED_IMAGE, WEBSITE_METADATA


@pytest.mark.parametrize('review', [False, True])
def test_unrecognized_image_is_readable(review):
    with pytest.raises(ValueError, match='Image format could not be recognized') as caught:
        fingerprint(b'not an image', review=review)
    assert str(caught.value) == UNRECOGNIZED_IMAGE


def test_website_metadata_is_not_a_photo():
    with pytest.raises(ValueError) as caught:
        fingerprint(b'vti_encoding:SR|utf8-nl\r\nvti_extenderversion:SR|4.0\r\n')
    assert str(caught.value) == WEBSITE_METADATA


def test_legacy_warning_normalizes_without_changing_other_errors():
    assert readable_warning('cannot identify image file <_io.BytesIO object at 0x123>') == UNRECOGNIZED_IMAGE
    assert readable_warning('Invalid EXIF orientation') == 'Invalid EXIF orientation'
    assert readable_warning(None) is None
