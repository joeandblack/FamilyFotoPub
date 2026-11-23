import asyncio

import pytest
from fastapi import HTTPException

from app.albums import collections as album_collections
from face_detect.api import people_preview_gateway


def test_disabling_the_gateway_reaches_album_people_reads(monkeypatch):
    """Read-only previews turn People off by replacing gateway.settings."""
    monkeypatch.setattr(people_preview_gateway, 'settings', lambda: None)
    with pytest.raises(HTTPException) as error:
        asyncio.run(album_collections.people_read(None, 'named-people'))
    assert error.value.status_code == 503
