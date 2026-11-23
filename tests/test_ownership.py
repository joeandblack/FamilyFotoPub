"""Original-import ownership in disposable PostgreSQL schemas; no real media."""
import os
import uuid
from contextlib import contextmanager
from unittest.mock import Mock,patch
import sys
import pytest
import psycopg
from psycopg import sql
from app import database
from app.config import load_config,ROOT

pytestmark=pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires local PostgreSQL')


@pytest.fixture
def catalog(monkeypatch):
    config=load_config();original=dict(config['database']);schema='fftest_'+uuid.uuid4().hex
    with psycopg.connect(**original) as db:db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setattr(database,'load_config',lambda:{**config,'database':{**original,'options':f'-c search_path={schema}'}})
    try:
        database.migrate()
        with database.connection() as db:
            for name in ('original','duplicate','third'):
                db.execute("INSERT INTO jobs(source_root,state,inventory_complete) VALUES(%s,'completed-review-required',true)",(name,))
            db.commit()
            yield db
    finally:
        with psycopg.connect(**original) as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def media(db,sha,owner=1,metadata='done',conversion=None,name='same.avi'):
    row=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,first_import_job_id,first_import_basis,conversion_state) VALUES(%s,%s,%s,'video',10,%s,'catalog-insert',%s) RETURNING id",(sha,sha+'.avi',name,owner,conversion)).fetchone()
    if metadata is not None:db.execute('INSERT INTO video_metadata(media_id,state) VALUES(%s,%s)',(row['id'],metadata))
    return row['id']


def link(db,job,id,state='duplicate'):
    db.execute('INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES(%s,%s,10,0,%s,%s)',(job,f'{job}/{id}/{uuid.uuid4().hex}.avi',state,id))


@pytest.mark.parametrize('metadata,conversion',[(None,None),('pending',None),('warning',None),('done',None),('done','done'),('done','warning'),('done','published'),('ancillary',None)])
def test_later_duplicates_never_own_counts_prerequisites_or_work(catalog,tmp_path,monkeypatch,metadata,conversion):
    from app.video import metadata as videos
    from app.video import conversions
    from app.video import avi
    from app.imports.history import history
    from app.jobs.job_scope import pending_work
    db=catalog;id=media(db,'shared',metadata=metadata,conversion=conversion)
    link(db,1,id,'imported');link(db,1,id);link(db,2,id);link(db,3,id);db.commit()
    summaries={row['id']:row for row in history(db)}
    for job in (2,3):
        assert summaries[job]['counts']=={'duplicate':1}
        assert summaries[job]['stages']=={'metadata':{},'conversion':{},'duplicates':{}}
        assert not pending_work(db,job,'metadata') and not pending_work(db,job,'conversion')
    assert sum(summaries[1]['stages']['metadata'].values())==1
    monkeypatch.setattr(conversions,'DATA',tmp_path)
    monkeypatch.setattr(conversions,'IMPORT_LOCK',uuid.uuid4().int%(2**63-1))
    conversions.check_ready(db,2)  # The owner's unfinished metadata cannot block a duplicate import.
    with patch.object(avi,'convert_one') as convert,patch.object(avi,'logger',return_value=Mock()):
        state={};avi.convert_queue(db,{},None,Mock(),state,lambda:None,tmp_path/'PAUSE',2)
        convert.assert_not_called();assert state['conversion_counts']=={'done':0,'warning':0,'duplicate':0,'pending':0}
    @contextmanager
    def connection():yield db
    for module in (videos,conversions):
        with patch.object(module,'connection',connection),patch.object(module,'load_config',side_effect=AssertionError('No disk/config work for duplicate-only import')):
            module.run(job_id=2)


def test_distinct_same_name_videos_and_completed_work(catalog,tmp_path):
    from app.video.avi import convert_queue
    from app.imports.history import history
    db=catalog;first=media(db,'hash-one');second=media(db,'hash-two',owner=2);done=media(db,'hash-done',conversion='done')
    link(db,1,first,'imported');link(db,2,first);link(db,2,second,'imported');link(db,1,done,'imported');link(db,2,done);db.commit()
    converted=[]
    def convert(db,row,*args):
        converted.append(row['id']);db.execute("UPDATE media SET conversion_state='done' WHERE id=%s",(row['id'],));db.commit()
    with patch('app.video.avi.convert_one',convert),patch('app.video.avi.logger',return_value=Mock()):
        convert_queue(db,{},None,Mock(),{},lambda:None,tmp_path/'PAUSE',2)
        convert_queue(db,{},None,Mock(),{},lambda:None,tmp_path/'PAUSE',2)
    assert converted==[second]
    summaries={r['id']:r for r in history(db)}
    assert summaries[1]['stages']['conversion']=={'pending':1,'done':1}
    assert summaries[2]['stages']['conversion']=={'done':1}


@pytest.mark.skipif(sys.platform=='darwin',reason='Legacy path-inferred source volumes are refused on macOS by design')
def test_importer_reimport_and_missing_copy_recovery_never_transfer_owner(catalog,tmp_path,monkeypatch):
    from app import telemetry
    from app.imports import importer
    from app.jobs.job_scope import pending_work
    db=catalog
    first=tmp_path/'original';later=tmp_path/'later';destination=tmp_path/'destination';data=tmp_path/'data'
    for folder in (first,later,destination,data):folder.mkdir()
    payload=b'isolated video payload'
    for folder in (first,later):(folder/'same.mp4').write_bytes(payload)
    for job,folder in ((1,first),(2,later)):
        db.execute("UPDATE jobs SET source_root=%s,state='scanning',inventory_complete=false WHERE id=%s",(str(folder),job))
    db.commit()
    config={'source':{'root':str(first)},'destination':{'root':str(destination)},'io_mibps':0}
    class Monitor:
        def __init__(self,config):self.thread=True
        def prepare(self):pass
        def check(self):pass
        def close(self):pass
    monkeypatch.setattr(importer,'load_config',lambda:config)
    monkeypatch.setattr('app.storage.config.enroll_source',lambda c,p:{**c['source'],'root':str(p)})
    monkeypatch.setattr(importer,'Monitor',Monitor);monkeypatch.setattr(importer,'DATA',data)
    monkeypatch.setattr(telemetry,'LOGS',tmp_path/'logs')
    monkeypatch.setattr(importer,'IMPORT_LOCK',uuid.uuid4().int%(2**63-1))
    monkeypatch.setattr(importer,'SINGLE_IMPORT_LOCK',uuid.uuid4().int%(2**63-1))
    importer.run();config['source']['root']=str(later);importer.run()
    row=db.execute('SELECT * FROM media').fetchone()
    assert row['first_import_job_id']==1 and row['first_import_basis']=='catalog-insert'
    assert db.execute('SELECT state FROM import_items WHERE job_id=2').fetchone()['state']=='duplicate'
    # Only a disposable target is removed to exercise established recovery.
    target=destination/row['relative_path'];target.unlink()
    db.execute("UPDATE import_items SET state='error' WHERE job_id=2");db.commit();importer.run()
    assert target.read_bytes()==payload
    assert db.execute('SELECT state FROM import_items WHERE job_id=2').fetchone()['state']=='duplicate'
    config['source']['root']=str(first)
    db.execute("UPDATE import_items SET state='pending' WHERE job_id=1");db.commit();importer.run()
    assert db.execute('SELECT first_import_job_id FROM media').fetchone()['first_import_job_id']==1
    assert pending_work(db,1,'metadata') and not pending_work(db,2,'metadata')
    assert (first/'same.mp4').read_bytes()==(later/'same.mp4').read_bytes()==payload


def test_legacy_backfill_requires_copy_and_creation_evidence_and_runs_once(catalog):
    db=catalog
    db.execute('DELETE FROM schema_versions WHERE version=8')
    db.execute("INSERT INTO job_events(job_id,event,created_at) VALUES(1,'running','2026-01-01T00:00:00Z'),(1,'completed-review-required','2026-01-01T00:05:00Z'),(2,'running','2026-01-01T00:10:00Z')")
    proven=media(db,'proven',owner=None);ambiguous=media(db,'ambiguous',owner=None);missing=media(db,'missing',owner=None);mismatch=media(db,'mismatch',owner=None)
    db.execute("UPDATE media SET created_at='2026-01-01T00:02:00Z'")
    # Inventory/duplicate order does not establish original ownership.
    link(db,2,proven);link(db,1,proven,'imported')
    link(db,1,ambiguous,'imported');link(db,2,ambiguous,'imported')
    link(db,2,missing);link(db,2,mismatch,'imported')
    db.execute("UPDATE jobs SET metadata_status='{\"scope\":\"import\",\"counts\":{\"done\":9}}' WHERE id=2")
    migration=(ROOT/'databases/008_original_import_ownership.sql').read_text()
    db.execute(migration)
    owners={r['id']:r['first_import_job_id'] for r in db.execute('SELECT id,first_import_job_id FROM media').fetchall()}
    assert owners=={proven:1,ambiguous:None,missing:None,mismatch:None}
    status=db.execute('SELECT metadata_status,legacy_postprocess_status FROM jobs WHERE id=2').fetchone()
    assert status['metadata_status'] is None and status['legacy_postprocess_status']['metadata']['counts']=={'done':9}
    # Later retries/history edits cannot silently backfill or transfer ownership.
    db.execute("UPDATE import_items SET state='duplicate' WHERE media_id=%s",(proven,))
    link(db,2,proven,'imported');link(db,1,missing,'imported');db.execute(migration)
    assert db.execute('SELECT first_import_job_id FROM media WHERE id=%s',(proven,)).fetchone()['first_import_job_id']==1
    assert db.execute('SELECT first_import_job_id FROM media WHERE id=%s',(missing,)).fetchone()['first_import_job_id'] is None
