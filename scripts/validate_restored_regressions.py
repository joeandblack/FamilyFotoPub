"""Run selected regression fixtures on the isolated restored PostgreSQL cluster."""
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
backup = Path(sys.argv[1]).resolve()
settings = json.loads((backup/'validation-connection.json').read_text())
if (settings['host'], settings['port'], settings['dbname']) != ('127.0.0.1', 55449, 'familyfoto_validation'):
    raise RuntimeError('Only the isolated validation database is allowed')
browser = '--browser' in sys.argv[2:]
import psycopg
with psycopg.connect(**settings) as db:
    actual = Path(db.execute('SHOW data_directory').fetchone()[0]).resolve()
    if actual != (backup/'validation-postgres').resolve():
        raise RuntimeError('Unexpected validation cluster')
from app import config
config.DATA = backup/'regression-state'
config.LOGS = config.DATA/'logs'
config.TEMP = config.DATA/'temp'
for folder in (config.DATA, config.LOGS, config.TEMP):
    folder.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(config.TEMP)
config.load_config = lambda: {'database': dict(settings), 'source': {}, 'destination': {}, 'host': '127.0.0.1'}
config.initialize_runtime = lambda: None
from app.service.readonly_preview import install_write_guard
if not browser:
    install_write_guard(backup)
else:
    # The opt-in fixture launches headless Chrome. It uses only synthetic media
    # and a disposable schema on the verified clone, never the live app.
    os.environ['FAMILYFOTO_BROWSER'] = '1'
    os.environ['FAMILYFOTO_BROWSER_OUTPUT'] = str(config.TEMP)
os.environ['FAMILYFOTO_INTEGRATION'] = '1'
import pytest
tests = ['tests/test_albums.py::test_browser_album_workflows',
         'tests/test_people_browser.py'] if browser else [
    'tests/test_albums.py', 'tests/test_album_collections.py',
    'tests/test_people_review_scope.py', 'tests/test_auth_security.py',
    'tests/test_readonly_preview.py',
    'tests/test_trash_integration.py', 'tests/test_crop.py',
    'tests/test_native_service_preferences.py',
    'tests/test_people_auto_organize.py',
    'tests/test_people_relaxed.py', 'tests/test_native_manual_matching.py',
]
report = 'browser-regressions' if browser else 'regressions'
raise SystemExit(pytest.main([
    '-q', '--tb=short', '-p', 'no:cacheprovider', '--basetemp='+str(backup/'pytest-runs'/uuid.uuid4().hex),
    '--log-file='+str(backup/(report+'.log')),
    '--junitxml='+str(backup/(report+'.xml')),
    *tests,
]))
