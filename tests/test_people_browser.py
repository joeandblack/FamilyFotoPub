"""Current People UI against real handlers in a disposable database schema."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app import database
from test_automatic_people import automatic, pool, env, observations
from face_detect.recognition import automatic_people as auto
from face_detect.api.automatic_people_api import router, reader, administrator


def test_people_browser_edits(automatic, tmp_path):
    if os.environ.get('FAMILYFOTO_BROWSER') != '1':
        pytest.skip('Requires installed Windows Chrome')
    with database.connection() as db:
        auto.initialize_review_sections(db, [])
        auto.ingest(db, 'browser', 'synthetic-auto', observations(automatic))
        groups = auto.catalog(db)['groups']
        named = groups[0]['id']
        unnamed = groups[1]['id']
        pid = auto.name_group(db, named, 'Browser Person', 'test')['person_id']
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[reader] = lambda: {'username': 'test', 'role': 'admin'}
    app.dependency_overrides[administrator] = app.dependency_overrides[reader]
    root = Path(__file__).resolve().parents[1]
    pixels = BytesIO()
    Image.new('RGB', (160, 120), (55, 135, 100)).save(pixels, format='JPEG')
    requests = []
    with TestClient(app) as client:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                path = urlsplit(self.path).path
                if path == '/' or path.startswith(('/web/', '/face_detect/web/')):
                    file = root / ('face_detect/web/automatic-people-preview.html' if path == '/' else path[1:])
                    assert file.resolve().is_relative_to(root)
                    body = file.read_bytes()
                    status = 200
                    mime = 'text/html' if file.suffix == '.html' else 'text/javascript'
                elif '/thumbnail' in path:
                    # Synthetic pixels only; mutations below use the actual handlers.
                    body, status, mime = pixels.getvalue(), 200, 'image/jpeg'
                else:
                    route = self.path.replace('/api/people-preview', '/api/automatic-people', 1)
                    content = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                    response = client.request(self.command, route, content=content,
                                              headers={'Content-Type': 'application/json'})
                    body, status = response.content, response.status_code
                    mime = response.headers.get('content-type', 'application/json')
                    requests.append({'method': self.command, 'path': route, 'status': status})
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = handle_request

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        fixture = tmp_path / 'people-browser.json'
        fixture.write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}/?integrated&desktop',
                                       'named': named, 'unnamed': unnamed, 'person': pid}))
        try:
            result = subprocess.run(['node', 'tests/people-edits-browser.mjs', str(fixture)],
                                    capture_output=True, text=True, timeout=120)
            (tmp_path / 'browser-result.txt').write_text(result.stdout + result.stderr)
            (tmp_path / 'requests.json').write_text(json.dumps(requests, indent=2))
            assert result.returncode == 0, result.stdout + result.stderr
            assert not [r for r in requests if r['method'] == 'POST' and r['status'] >= 400]
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
