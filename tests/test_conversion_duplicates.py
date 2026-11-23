import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.video import avi
from app.library import trash
from app.jobs.job_scope import pending_work
from app.imports.history import history
from test_ownership import catalog,pytestmark
from test_trash import fixture


def seed(db,root):
    output=b'identical encoded video';original=b'different original AVI'
    sha=hashlib.sha256(output).hexdigest();relative=f'{sha[:2]}/{sha[2:4]}/{sha}.mp4'
    target=root/relative;target.parent.mkdir(parents=True);target.write_bytes(output)
    (root/'second.avi').write_bytes(original)
    first=db.execute("INSERT INTO media(sha256,stored_sha256,relative_path,name,kind,size,conversion_state,first_import_job_id) VALUES(%s,%s,%s,'first.avi','video',%s,'done',1) RETURNING *",('a'*64,sha,relative,len(output))).fetchone()
    second=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,first_import_job_id) VALUES(%s,'second.avi','second.avi','video',%s,1) RETURNING *",(hashlib.sha256(original).hexdigest(),len(original))).fetchone()
    db.execute('UPDATE media SET original_relative_path=%s WHERE id=%s',('first-original.avi',first['id']))
    for row in (first,second):db.execute("INSERT INTO video_metadata(media_id,state) VALUES(%s,'done')",(row['id'],))
    db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES(1,'original-second.avi',%s,0,'imported',%s)",(len(original),second['id']))
    db.commit();return first,second,sha,relative,output


def test_duplicate_atomic_retry_restore_and_delete(catalog,tmp_path,monkeypatch):
    root,config,_=fixture(tmp_path,monkeypatch);db=catalog
    monkeypatch.setattr(trash,'DATA',tmp_path)
    monkeypatch.setattr(trash,'WORKER_LOCK',uuid.uuid4().int%(2**63-1))
    first,second,sha,relative,output=seed(db,root)
    # Crash after linking but before Trash insertion must roll back both.
    class Crash:
        def execute(self,query,args=None):
            if query.startswith('INSERT INTO media_trash'):raise OSError('simulated crash')
            return db.execute(query,args)
        def commit(self):db.commit()
    with pytest.raises(OSError,match='simulated crash'):
        avi.mark_output_duplicate(Crash(),second,relative,sha,len(output),str(root),lambda:None)
    db.rollback()
    assert db.execute('SELECT conversion_state FROM media WHERE id=%s',(second['id'],)).fetchone()['conversion_state'] is None
    assert avi.mark_output_duplicate(db,second,relative,sha,len(output),str(root),lambda:None)
    assert avi.mark_output_duplicate(db,second,relative,sha,len(output),str(root),lambda:None)
    row=db.execute('SELECT * FROM media WHERE id=%s',(second['id'],)).fetchone()
    assert row['conversion_duplicate_of']==first['id'] and row['duplicate_output_sha256']==sha
    assert row['relative_path']==second['relative_path'] and row['sha256']==second['sha256'] and row['stored_sha256'] is None
    assert row['first_import_job_id']==1 and db.execute('SELECT media_id FROM import_items').fetchone()['media_id']==second['id']
    assert history(db)[-1]['stages']['conversion']=={'done':1,'duplicate':1}
    assert not pending_work(db,1,'conversion')
    trash.change(db,second['id'],'restore',config);db.commit()
    assert not pending_work(db,1,'conversion')
    avi.convert_one(db,row,config,{},SimpleNamespace(check=lambda:None))
    assert not db.execute('SELECT 1 FROM media_trash WHERE media_id=%s',(second['id'],)).fetchone()
    # Restored item keeps its original hash for future original-import dedup.
    assert db.execute('SELECT id FROM media WHERE sha256=%s',(second['sha256'],)).fetchone()['id']==second['id']
    trash.change(db,second['id'],'trash',config);db.commit()
    trash.change(db,second['id'],'delete',config);db.commit()
    assert (root/relative).read_bytes()==output and not (root/'second.avi').exists()
    assert db.execute('SELECT conversion_duplicate_of FROM media WHERE id=%s',(second['id'],)).fetchone()['conversion_duplicate_of']==first['id']


@pytest.mark.parametrize('problem',['checksum','trashed','unpublished','shared-delete'])
def test_collision_guards(catalog,tmp_path,monkeypatch,problem):
    root,config,_=fixture(tmp_path,monkeypatch);db=catalog
    first,second,sha,relative,output=seed(db,root)
    if problem=='checksum':(root/relative).write_bytes(b'corrupted')
    if problem=='trashed':db.execute('INSERT INTO media_trash(media_id) VALUES(%s)',(first['id'],))
    if problem=='unpublished':db.execute('UPDATE media SET conversion_state=NULL WHERE id=%s',(first['id'],))
    if problem=='shared-delete':
        second.update(conversion_duplicate_of=first['id'],relative_path=relative)
        with pytest.raises(OSError,match='shares a retained'):trash.delete_files(db,second,config)
    else:
        with pytest.raises(OSError):avi.mark_output_duplicate(db,second,relative,sha,len(output),str(root),lambda:None)
    db.rollback()
    assert (root/'second.avi').exists() and (root/relative).exists()


def test_convert_one_collision_keeps_avi_and_staging(catalog,tmp_path,monkeypatch):
    from app.video import metadata as videos
    root,config,_=fixture(tmp_path,monkeypatch);db=catalog
    first,second,sha,relative,output=seed(db,root)
    config['avi_conversion']={'remove_destination_avi':True}
    data=tmp_path/'data';data.mkdir();monkeypatch.setattr(avi,'DATA',data)
    probe={'format':{'duration':'1'},'streams':[{'codec_type':'video','codec_name':'h264','width':2,'height':2}]}
    monkeypatch.setattr(avi,'probe',lambda *args:probe)
    def decoder(args,**kwargs):
        if '-c:v' in args:(data/'conversion-temp'/str(second['id'])/'output.mp4').write_bytes(output)
        return '{}'
    monkeypatch.setattr(videos,'run_tool',decoder)
    avi.convert_one(db,second,config,{'ffmpeg':'fixture'},SimpleNamespace(check=lambda:None))
    assert (root/'second.avi').exists() and (root/relative).read_bytes()==output
    assert (data/'conversion-temp'/str(second['id'])/'output.mp4').exists()
    assert db.execute('SELECT conversion_state FROM media WHERE id=%s',(second['id'],)).fetchone()['conversion_state']=='duplicate'
