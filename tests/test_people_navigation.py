from types import SimpleNamespace
import pytest
from app.auth.api import me
from face_detect.api import people_preview_gateway


@pytest.mark.parametrize('existing,native,role,authenticated,expected', [
    (True,False,'admin',True,True),
    (False,True,'admin',True,True),
    (False,False,'admin',True,False),
    (True,False,'guest',True,True),
    (True,False,'regular',True,True),
    (False,True,'regular',True,True),
    (True,False,'admin',False,False),
])
def test_people_navigation_is_independent_of_native_startup(monkeypatch,existing,native,role,authenticated,expected):
    monkeypatch.setattr(people_preview_gateway,'settings',lambda: {} if native else None)
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(existing_people_catalog=existing)),
                            state=SimpleNamespace(user={'role':role},authenticated=authenticated))
    assert me(request)['capabilities']['people_preview'] is expected
