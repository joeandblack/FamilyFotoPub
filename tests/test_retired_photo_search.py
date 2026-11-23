"""Retiring the search experiment must not remove supported People routes."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from face_detect.api.automatic_people_api import router


def test_photo_search_endpoint_is_removed():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.post('/api/automatic-people/search-photos',
                           json={'job_ids': [1]}).status_code == 404
    paths = {route.path for route in router.routes}
    assert '/api/automatic-people/cards' in paths
    assert '/api/automatic-people/review-page' in paths
