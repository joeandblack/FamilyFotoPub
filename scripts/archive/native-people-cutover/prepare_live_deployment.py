"""Prepare normal writable deployment without launching it or resuming work."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

import psycopg

ROOT=Path(__file__).resolve().parents[3]
SOURCE=Path('D:/PROJECTS/FamilyFoto')
BACKUP=Path('D:/FamilyFoto-backups/20261030-pre-write-validation')


def main():
    if not json.loads((BACKUP/'manifest.json').read_text())['stages'].get('backup_complete'):
        raise RuntimeError('Verified backup required')
    source=json.loads((SOURCE/'config/local.json').read_text())
    with psycopg.connect(**source['database']) as db:
        db.execute('SET TRANSACTION READ ONLY')
        queued=db.execute("SELECT count(*) FROM upload_sessions WHERE state IN ('queued','importing','cancelling') OR (state='done' AND cleaned_at IS NULL)").fetchone()[0]
        if queued:raise RuntimeError('Upload queue changed; review before deployment')
        if db.execute("SELECT count(*) FROM jobs WHERE state NOT IN ('completed-review-required','completed','cancelled')").fetchone()[0]:
            raise RuntimeError('An import is unfinished')
    settings=SOURCE/'data/face-service-settings.json'
    if json.loads(settings.read_text()).get('start_with_app') is not False:
        raise RuntimeError('Face startup must explicitly be disabled')
    target=BACKUP/('live-cutover-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    target.mkdir()
    old=ROOT/'config/local.json'
    if old.exists():shutil.copy2(old,target/'previous-new-checkout-config.json')
    shutil.copy2(settings,target/'face-service-settings.json')
    # Refresh the database backup after any edits made in limited-live mode.
    pg=SOURCE/'runtime/postgres/bin'
    admin={**source['database'],'user':'postgres','password':source['postgres_admin_password']}
    env={**os.environ,'PGPASSWORD':admin['password']}
    dump=target/'familyfoto.dump'
    command=[str(pg/'pg_dump.exe'),'-h',str(admin['host']),'-p',str(admin['port']),
             '-U',admin['user'],'-d',admin['dbname'],'-Fc','-f',str(dump)]
    subprocess.run(command,env=env,check=True,capture_output=True)
    subprocess.run([str(pg/'pg_restore.exe'),'--list',str(dump)],check=True,capture_output=True)
    with dump.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    source.pop('postgres_admin_password',None)
    source['live_existing_catalog']=True
    source['preview']={'data_root':str(SOURCE/'data')}
    source['video_tools']={name:str(SOURCE/'runtime/ffmpeg/bin'/f'{name}.exe') for name in ('ffmpeg','ffprobe')}
    for path in source['video_tools'].values():
        if not Path(path).is_file():raise RuntimeError('Required media tool unavailable')
    for key in ('certfile','keyfile'):
        path=Path(source['tls'][key])
        source['tls'][key]=str(path if path.is_absolute() else SOURCE/path)
    old.write_text(json.dumps(source,indent=2),encoding='utf-8')
    user=subprocess.check_output(['whoami'],text=True).strip()
    subprocess.run(['icacls',str(old),'/inheritance:r','/grant:r',user+':(F)',
                    '*S-1-5-18:(F)','*S-1-5-32-544:(F)'],check=True,capture_output=True)
    (target/'manifest.json').write_text(json.dumps({'database_dump_bytes':dump.stat().st_size,
        'sha256':digest,'data_root':str(SOURCE/'data'),'face_start_with_app':False},indent=2))
    print('Live configuration prepared; refreshed backup: '+str(target))


if __name__=='__main__':main()
