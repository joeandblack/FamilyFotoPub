import pytest
from app.jobs.limited_edit import allowed_write


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/people-preview/groups/1/selected-faces'),
    ('POST', '/api/people-preview/review-actions/stream'),
    ('POST', '/api/people-preview/events/4/undo'),
    ('POST', '/api/albums/1/items'), ('PATCH', '/api/albums/1'),
    ('DELETE', '/api/albums/1'), ('POST', '/api/album-collections/1/people'),
])
def test_explicit_metadata_routes(method, path):
    assert allowed_write(method, path)


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/people-preview/jobs'), ('POST', '/api/people-preview/auto-organize/stream'),
    ('POST', '/api/people-preview/blurry/1/review'), ('POST', '/api/media/1/trash/trash'),
    ('DELETE', '/api/media/1'), ('POST', '/api/import/start'),
    ('POST', '/api/face-service/start'), ('PATCH', '/api/settings'),
    ('POST', '/api/people-preview/groups/1/name/extra'),
    ('GET', '/api/albums/1'), ('POST', '/api/albums/1/arbitrary'),
])
def test_other_mutations_blocked(method, path):
    assert not allowed_write(method, path)
