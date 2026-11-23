"""The router modules bind config and database names separately; patch them together."""
from app import api, system_api
from app.auth import api as auth_api
from app.library import api as media_api
from app.imports import api as imports_api

CONFIG_MODULES = (api, auth_api, system_api, media_api, imports_api)
DATA_MODULES = (system_api, media_api, imports_api)


def patch_all(monkeypatch, modules, name, value):
    for module in modules:
        monkeypatch.setattr(module, name, value)
