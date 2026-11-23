"""Guest-only HTTP checks against the disposable Core installation, never live data."""
import json
import os
from pathlib import Path
import time

import httpx


def main():
    if os.environ.get('USERNAME') != 'WDAGUtilityAccount' or Path(__file__).resolve().parents[2] != Path('C:/FamilyFoto'):
        raise SystemExit('Disposable Sandbox only')
    results = []
    output = Path('C:/TestResults/smoke.json')

    def record(name, passed, detail=''):
        results.append({'check': name, 'passed': passed, 'detail': detail})
        output.write_text(json.dumps(results, indent=2), encoding='utf-8')

    # Our own loopback server inside the disposable sandbox; its private CA is not trusted here.
    with httpx.Client(base_url='https://127.0.0.1:4340', timeout=30, verify=False) as client:
        for _ in range(60):
            try:
                if client.get('/login').status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(2)
        else:
            record('first launch', False)
            return 1
        record('first launch', True)
        record('unauthenticated library denied', client.get('/api/media').status_code == 401)
        password = Path('C:/TestCredentials/password.txt').read_text().strip()
        login = client.post('/api/login', json={'username': 'family', 'password': password})
        record('login', login.status_code == 200)
        if login.status_code != 200:
            return 1
        for endpoint in ('/', '/api/media', '/api/import/history', '/api/albums', '/api/storage/settings'):
            response = client.get(endpoint)
            record(endpoint, response.status_code == 200, f'HTTP {response.status_code}')
        response = client.post('/api/albums', json={'name': 'Sandbox test album', 'description': 'Synthetic installer check'})
        record('create album', response.status_code == 200, f'HTTP {response.status_code}')
        settings = client.get('/api/storage/settings').json()
        record('storage deferred until import', not settings.get('source') and not settings.get('destination'))
        source = Path('C:/SandboxMediaSource')
        source.mkdir(exist_ok=True)
        from PIL import Image
        Image.new('RGB', (320, 240), (35, 120, 80)).save(source / 'installer-test.jpg')
        storage = client.post('/api/storage/settings', json={
            'source': str(source), 'destination': 'C:/SandboxMediaLibrary', 'revision': settings['revision']})
        if storage.status_code == 409 and any(word in storage.text.lower() for word in ('disk identity', 'disk is disconnected')):
            record('media import', None, 'Sandbox disk identity is unavailable; full VM storage/import test required. Safety checks were not bypassed.')
        else:
            record('first import storage enrollment', storage.status_code == 200, f'HTTP {storage.status_code}')
            if storage.status_code == 200:
                started = client.post('/api/import/start', json={'source': str(source)})
                record('start synthetic import', started.status_code == 200)
                if started.status_code == 200:
                    for _ in range(90):
                        media = client.get('/api/media').json()
                        if media.get('items'):
                            item = media['items'][0]
                            record('imported thumbnail', client.get(f"/api/media/{item['id']}/thumbnail").status_code == 200)
                            record('imported original', client.get(f"/api/media/{item['id']}/original").status_code == 200)
                            break
                        time.sleep(2)
                    else:
                        record('synthetic import completed', False)
    return 0 if all(row['passed'] is not False for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
