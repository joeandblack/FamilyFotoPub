import pytest
from face_detect.migration import correspond


def old(did=1, x=.1):
    return {'id': did, 'box': {'x': x, 'y': .1, 'width': .2, 'height': .2}}


def new(x=10):
    return {'box': {'x_min': x, 'y_min': 10, 'x_max': x+20, 'y_max': 30}}


def test_exact_correspondence_preserves_id():
    assert correspond([old(42)], [new()], (100, 100)) == {42: {'index': 0, 'overlap': pytest.approx(1)}}


def test_missing_face_is_not_reassigned():
    assert correspond([old()], [new(60)], (100, 100)) == {}


def test_ambiguous_native_faces_are_not_used():
    assert correspond([old()], [new(), new(11)], (100, 100)) == {}


def test_two_old_faces_cannot_share_native_detection():
    assert correspond([old(1), old(2)], [new()], (100, 100)) == {}


def test_invalid_legacy_box_remains_unmatched():
    face=old(); face['box']['width']=-1
    assert correspond([face], [new()], (100, 100)) == {}


def test_invalid_native_geometry_rejected():
    with pytest.raises(ValueError, match='Invalid native'):
        correspond([old()], [new(95)], (100, 100))


def test_no_detections_does_not_remove_anything():
    assert correspond([old()], [], (100, 100)) == {}
