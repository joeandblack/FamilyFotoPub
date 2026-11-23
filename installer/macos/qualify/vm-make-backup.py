"""Inside a test VM, through FamilyFoto's own API: a small library, then a real verified backup.

Run with the installation's Python after vm-auto-test.sh installed and started ~/FamilyFoto:
  ~/FamilyFoto/.venv/bin/python vm-make-backup.py BACKUP-VOLUME < passwords
stdin: line 1 the administrator password set at install, line 2 a short password to switch to
(the backup is then made with it, so it can be typed in a VM window). Never printed.
Never run on a real Mac.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

HOME = Path.home()
ROOT = HOME / 'FamilyFoto'
PHOTOS, LIBRARY = HOME / 'ff-photos', HOME / 'FFLibrary'
volume = Path(sys.argv[1])
install_password, short_password = (sys.stdin.readline().rstrip('\n') for _ in range(2))
web = httpx.Client(base_url='https://127.0.0.1:4341', verify=False, timeout=60,
                   headers={'Origin': 'https://127.0.0.1:4341'})


def call(method, path, **kwargs):
    for _ in range(120):  # the web server stops and comes back during a backup
        try:
            response = web.request(method, path, **kwargs)
            if response.status_code not in (502, 503):
                return response
        except httpx.TransportError:
            pass
        time.sleep(2)
    raise SystemExit(f'{method} {path}: FamilyFoto did not answer')


def ok(response, what):
    if response.status_code >= 400:
        raise SystemExit(f'{what} failed ({response.status_code}): {response.text[:300]}')
    return response.json()


def login(password):
    ok(call('POST', '/api/login', json={'username': 'family', 'password': password}), 'sign-in')


def step(text):
    print('== ' + text, flush=True)


step('sign in, then switch to the short password')
login(install_password)
ok(call('POST', '/api/auth/users/password', json={'username': 'family', 'password': short_password,
                                                    'admin_password': install_password}), 'password change')
login(short_password)

step('test photos and a video')
PHOTOS.mkdir(exist_ok=True)
from PIL import Image  # noqa: E402
import pillow_heif  # noqa: E402
for i, colour in enumerate(((200, 40, 40), (40, 160, 60), (40, 70, 200))):
    Image.new('RGB', (640, 480), colour).save(PHOTOS / f'photo-{i + 1}.jpg', quality=90)
pillow_heif.from_pillow(Image.new('RGB', (640, 480), (230, 180, 30))).save(PHOTOS / 'photo-4.heic')
subprocess.run([str(ROOT / 'bundled/ffmpeg/bin/ffmpeg'), '-loglevel', 'error', '-y', '-f', 'lavfi',
                '-i', 'testsrc=duration=3:size=640x360:rate=25', '-pix_fmt', 'yuv420p', str(PHOTOS / 'clip.mp4')],
               check=True)
print(f'   {len(list(PHOTOS.iterdir()))} files in {PHOTOS}')

step('library folder and import')
revision = ok(call('GET', '/api/storage/settings'), 'storage settings')['revision']
ok(call('POST', '/api/storage/settings', json={'source': str(PHOTOS), 'destination': str(LIBRARY),
                                                 'revision': revision}), 'storage save')
ok(call('POST', '/api/import/start', json={'source': str(PHOTOS), 'auto_metadata': True,
                                             'auto_conversion': True, 'auto_duplicates': True}), 'import start')
for _ in range(180):
    time.sleep(2)
    overview = ok(call('GET', '/api/backup'), 'backup overview')
    if overview['media_bytes'] and not overview['busy']:
        break
else:
    raise SystemExit('the import did not finish in 6 minutes')
print(f"   imported: {overview['media_bytes']} bytes of media")

step(f'backup to {volume}')
plan = ok(call('POST', '/api/backup/plan', json={'destination': str(volume), 'password': short_password}), 'plan')
if plan['problems']:
    raise SystemExit('backup plan refused: ' + '; '.join(plan['problems']))
started = time.monotonic()
run = ok(call('POST', '/api/backup/start', json={'destination': str(volume), 'password': short_password,
                                                   'confirm_stop': True, 'allow_unprotected': bool(plan.get('unprotected'))}),
         'backup start')['run']
while True:
    time.sleep(3)
    response = call('GET', '/api/backup-run')
    if response.status_code == 401:
        login(short_password)
        continue
    status = ok(response, 'backup status')
    if status.get('finished') or status.get('state') == 'failed':
        break
latest = status  # this client holds the run's cookie, so it sees the whole record
print(f"   finished in {time.monotonic() - started:.0f} s: " + json.dumps(
    {k: latest.get(k) for k in ('state', 'verified_at', 'directory', 'error', 'media_files')}))
folders = sorted(p.name for p in volume.iterdir() if p.is_dir() and (p / 'catalog').is_dir())
print('   backup folders: ' + ', '.join(folders))
if not folders or not latest.get('verified_at'):
    raise SystemExit('the backup did not finish verified')
handover = sorted(p.name for p in (volume / folders[-1] / 'restore-on-mac').iterdir())
print('   restore-on-mac: ' + ', '.join(handover))
