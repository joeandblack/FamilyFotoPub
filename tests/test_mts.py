import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from app import database
from app.video import avi
from app.video import mts
from app.video import metadata as videos
from app.video import converter as video_converter
from test_uploads import env
from test_unexamined_photos import disposable_migrations


def test_mts_plan():
    probe={'streams':[{'codec_type':'video','codec_name':'h264','width':1440,'height':1080,'pix_fmt':'yuv420p'},
                      {'codec_type':'audio','codec_name':'ac3'}]}
    selected=mts.plan(probe)
    assert selected['method']=='audio-only' and selected['video']=='copy'
    assert selected['audio']=='aac' and selected['profile']['preserve_original']
    args=mts.command({'ffmpeg':'ffmpeg'},'in.mts','out.mp4',selected)
    assert args[args.index('-format_whitelist')+1]=='mpegts'
    assert args[args.index('-c:v')+1]=='copy'


@pytest.mark.parametrize('validation',['full','quick'])
def test_mts_publication_and_resume(env,monkeypatch,validation):
    _,config,data=env
    config['avi_conversion']={'enabled':True,'remove_destination_avi':True}
    config['conversion_performance']={'io_mibps':0,'retained_original_validation':validation}
    monkeypatch.setattr(avi,'DATA',data);monkeypatch.setattr(videos,'DATA',data)
    tools=videos.tool_paths();root=Path(config['destination']['root']);source=root/'fixture.mts'
    subprocess.run([tools['ffmpeg'],'-nostdin','-n','-v','error','-f','lavfi','-i','testsrc2=size=64x48:rate=25',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2',
                    '-c:v','libx264','-pix_fmt','yuv420p','-threads','1','-c:a','ac3','-f','mpegts',str(source)],check=True,capture_output=True,timeout=30)
    original=source.read_bytes();sha=hashlib.sha256(original).hexdigest()
    run_tool=videos.run_tool
    commands=[]
    def recorded(command,**kwargs):
        commands.append(command)
        return run_tool(command,**kwargs)
    monkeypatch.setattr(videos,'run_tool',recorded)
    with database.connection() as db:
        job=db.execute("INSERT INTO jobs(source_root,state,inventory_complete) VALUES('mts-fixture','completed-review-required',true) RETURNING id").fetchone()['id']
        media=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,first_import_job_id) VALUES(%s,'fixture.mts','fixture.mts','video',%s,%s) RETURNING *",(sha,len(original),job)).fetchone()
        db.execute("INSERT INTO video_metadata(media_id,state,metadata) VALUES(%s,'done','{}')",(media['id'],));db.commit()
        from app.jobs.job_scope import pending_work
        from app.imports.history import history
        assert pending_work(db,job,'conversion')
        assert next(r for r in history(db) if r['id']==job)['stages']['conversion']=={'pending':1}
        class Interrupted:
            def execute(self,query,args=None):
                if "conversion_state='done'" in query or "SET conversion_state='warning'" in query:
                    raise OSError('Fixture interruption')
                return db.execute(query,args)
            def commit(self):db.commit()
        with pytest.raises(OSError,match='Fixture interruption'):
            video_converter.convert_one(Interrupted(),media,config,tools,SimpleNamespace(check=lambda:None))
        db.rollback()
        row=db.execute('SELECT * FROM media WHERE id=%s',(media['id'],)).fetchone();db.commit()
        assert row['conversion_state']=='published' and row['original_relative_path']=='fixture.mts'
        assert source.read_bytes()==original
        assert hashlib.sha256((root/row['relative_path']).read_bytes()).hexdigest()==row['stored_sha256']
        metadata=db.execute('SELECT metadata FROM video_metadata WHERE media_id=%s',(media['id'],)).fetchone()['metadata']
        assert metadata['conversion_validation']['mode']==validation
        decode_commands=[c for c in commands if 'null' in c]
        assert len(decode_commands)==(0 if validation=='quick' else 3)
        if validation=='quick':assert 'frame_validation' not in metadata
        # Recovery must use the persisted policy, not newly edited settings.
        config['conversion_performance']['retained_original_validation']='full' if validation=='quick' else 'quick'
        monkeypatch.setattr(videos,'run_tool',lambda *a,**kw:pytest.fail('Resume must not decode again'))
        video_converter.convert_one(db,row,config,tools,SimpleNamespace(check=lambda:None))
        assert not pending_work(db,job,'conversion')
        assert source.read_bytes()==original
        final=db.execute('SELECT conversion_state,conversion_error FROM media WHERE id=%s',(media['id'],)).fetchone()
        assert final['conversion_state']=='done'
        if validation=='quick':
            assert final['conversion_error'] is None
            from app.imports.diagnostics import diagnostics
            assert not any('full playback verification was skipped' in (i.get('error') or '') for i in diagnostics(db,job))


@pytest.mark.parametrize('settings',[{'io_mibps':-1},{'io_mibps':float('inf')},{'io_mibps':True},
                                     {'retained_original_validation':'skip-everything'}])
def test_invalid_performance_policy(settings):
    from app.video.conversion_performance import policy
    with pytest.raises(ValueError):policy({'conversion_performance':settings})


def test_quick_mode_still_rejects_corrupt_input_before_decoding(tmp_path,monkeypatch):
    data=tmp_path/'data';data.mkdir()
    source=tmp_path/'original.mts';source.write_bytes(b'changed media bytes')
    monkeypatch.setattr(avi,'DATA',data)
    monkeypatch.setattr(videos,'run_tool',lambda *a,**kw:pytest.fail('Unverified input must not reach a decoder'))
    config={'destination':{'root':str(tmp_path)},'conversion_performance':{'io_mibps':0,'retained_original_validation':'quick'}}
    media={'id':1,'relative_path':'original.mts','size':source.stat().st_size,'sha256':'a'*64}
    with pytest.raises(OSError,match='SHA-256'):
        mts.convert_one(None,media,config,{},SimpleNamespace(check=lambda:None))
    assert source.read_bytes()==b'changed media bytes'
    assert not (data/'conversion-temp/1/output.mp4').exists()
