"""Explicit, stopped-server activation; retains the complete legacy rollback."""
import argparse
import json
from pathlib import Path
import shutil
import socket
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # prepare_people_migration stays in scripts/
from prepare_people_migration import ROOT, save
from verify_people_migration import copy_thumbnail
from concurrent.futures import ThreadPoolExecutor


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--migration',type=Path,required=True)
    args=parser.parse_args();folder=args.migration.resolve()
    published=json.loads((folder/'published-live.json').read_text())
    runtime=json.loads((folder/'native-runtime.json').read_text())
    if not published['complete'] or published['schema']!=runtime['schema']:
        raise ValueError('Verified live schema publication required')
    path=ROOT/'config/local.json';cfg=json.loads(path.read_text(encoding='utf-8'))
    with socket.socket() as probe:
        host=cfg.get('host','127.0.0.1')
        if host=='0.0.0.0':host='127.0.0.1'
        if probe.connect_ex((host,cfg['port']))==0:raise RuntimeError('Stop FamilyFoto before activation')
    data=Path(cfg['preview']['data_root'])
    root=data/'face_detect/profiles'/runtime['model_id'][:32]
    if root.exists():raise ValueError('Profile directory already exists; do not overwrite')
    shutil.copy2(path,folder/'before-native-local.json')
    shutil.copy2(data/'people-preview.json',folder/'before-native-people-preview.json')
    root.mkdir(parents=True)
    destination=root/'thumbnails';destination.mkdir()
    with ThreadPoolExecutor(max_workers=8) as pool:
        hashes=dict(pool.map(lambda source:copy_thumbnail(source,destination),(folder/'thumbnails').glob('*.jpg')))
    if hashes!=json.loads((folder/'thumbnail-hashes.json').read_text()):
        raise ValueError('Published thumbnails differ from verified preservation copy')
    legacy=Path(json.loads((folder/'migration.json').read_text())['source_profile'])
    for parent in (legacy/'jobs').glob('import-*/import.json'):
        out=root/'jobs'/parent.parent.name;out.mkdir(parents=True,exist_ok=True)
        shutil.copy2(parent,out/'import.json')
        phase=json.loads(parent.read_text()).get('state')
        save(out/'control.json',{'action':'cancel' if phase=='cancelled' else 'pause'})
    save(root/'runtime.json',runtime)
    with psycopg.connect(**cfg['database'],row_factory=dict_row) as db:
        links=db.execute('SELECT * FROM album_collection_people WHERE pilot=%s FOR UPDATE',(runtime['legacy_pilot'],)).fetchall()
        save(folder/'before-native-collection-links.json',links)
        ids=sorted({row['person_id'] for row in links})
        count=db.execute(sql.SQL('SELECT count(*) n FROM {}.people WHERE id=ANY(%s)').format(
            sql.Identifier(runtime['schema'])),(ids,)).fetchone()['n']
        if count!=len(ids):raise ValueError('Collection identity missing from native catalog')
        db.execute('UPDATE album_collection_people SET pilot=%s WHERE pilot=%s',(root.name,runtime['legacy_pilot']))
    cfg['live_native_catalog']=True
    cfg['face_detection']={'active_profile':'windows_insightface','platform_defaults':{'win32':'windows_insightface'},
                           'profiles':{'windows_insightface':runtime['profile']}}
    # Server is stopped. An in-place write retains the existing Windows secret ACL.
    path.write_text(json.dumps(cfg,indent=2),encoding='utf-8')
    save(data/'people-preview.json',{'enabled':True,'backend':'native','pilot':root.name,'port':54341})
    save(data/'face-service-settings.json',{'start_with_app':False})
    save(folder/'activation.json',{'configured':True,'server_started':False,'native_catalog':root.name,
                                 'legacy_catalog_retained':runtime['legacy_pilot'],'automatic_start':False,
                                 'collection_links_preserved':len(links)})
    print('Native catalog selected. Legacy catalog retained. All saved jobs paused; automatic face startup disabled.')


if __name__=='__main__':main()
