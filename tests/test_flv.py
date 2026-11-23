import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import database
from app.video import avi
from app.video import flv
from app.video import metadata as videos
from app.video import converter as video_converter
from app.library.media import kind_for
from test_uploads import env


def probe(video='vp6f', audio='mp3'):
    streams=[{'codec_type':'video','codec_name':video,'width':640,'height':480,'pix_fmt':'yuv420p'}]
    if audio:streams.append({'codec_type':'audio','codec_name':audio})
    return {'streams':streams}


@pytest.mark.parametrize('video,audio,method',[
    ('vp6f','mp3','transcode'),('flv1','mp3','transcode'),
    ('h264','aac','remux'),('h264',None,'remux'),('h264','mp3','audio-only')])
def test_plan(video,audio,method):
    plan=flv.plan(probe(video,audio))
    assert plan['method']==method
    assert plan['profile']['preserve_original']
    args=flv.command({'ffmpeg':'ffmpeg'},'input.flv','output.mp4',plan)
    assert '-n' in args and '-r' not in args
    assert args[args.index('-c:v')+1]==('libx264' if method=='transcode' else 'copy')
    if method=='transcode':assert args[args.index('-fps_mode')+1]=='passthrough'


@pytest.mark.parametrize('change',['odd','rotation','multiple','high-depth'])
def test_ambiguous_or_incompatible_streams(change):
    value=probe('h264','aac')
    if change=='odd':value['streams'][0]['width']=641
    if change=='rotation':value['streams'][0]['side_data_list']=[{'rotation':90}]
    if change=='multiple':value['streams'].append(copy.deepcopy(value['streams'][0]))
    if change=='high-depth':
        value['streams'][0]['pix_fmt']='yuv420p10le'
        assert flv.plan(value)['method']=='transcode'
    else:
        with pytest.raises(ValueError):flv.plan(value)


def test_flv_importable_and_poster_allowed():
    assert kind_for(Path('example.FLV'))=='video'
    assert 'flv' in videos.FORMATS.split(',')


@pytest.mark.parametrize('validation',['full','quick'])
@pytest.mark.parametrize('video,audio,method',[
    ('flv','libmp3lame','transcode'),('libx264','aac','remux'),('libx264','libmp3lame','audio-only')])
def test_real_ffmpeg_publication_and_resume(env,monkeypatch,video,audio,method,validation):
    _,config,data=env
    config['avi_conversion']={'enabled':True,'remove_destination_avi':True}
    config['conversion_performance']={'io_mibps':0,'retained_original_validation':validation}
    monkeypatch.setattr(avi,'DATA',data)
    monkeypatch.setattr(videos,'DATA',data)
    tools=videos.tool_paths()
    root=Path(config['destination']['root']);source=root/'fixture.flv'
    subprocess.run([tools['ffmpeg'],'-nostdin','-n','-v','error',
        '-f','lavfi','-i','testsrc2=size=64x48:rate=10',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','1.2',
        '-c:v',video,'-pix_fmt','yuv420p','-threads','1','-c:a',audio,'-f','flv',str(source)],
        check=True,timeout=30,capture_output=True)
    original=source.read_bytes();sha=hashlib.sha256(original).hexdigest()
    with database.connection() as db:
        media=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES(%s,'fixture.flv','fixture.flv','video',%s) RETURNING *",(sha,len(original))).fetchone()
        db.execute("INSERT INTO video_metadata(media_id,state,metadata) VALUES(%s,'done','{}')",(media['id'],));db.commit()
        class Interrupted:
            def execute(self,query,args=None):
                if "conversion_state='done'" in query or "SET conversion_state='warning'" in query:
                    raise OSError('Simulated power loss after publication')
                return db.execute(query,args)
            def commit(self):db.commit()
        with pytest.raises(OSError,match='power loss'):
            video_converter.convert_one(Interrupted(),media,config,tools,SimpleNamespace(check=lambda:None))
        db.rollback()
        row=db.execute('SELECT * FROM media WHERE id=%s',(media['id'],)).fetchone();db.commit()
        assert row['conversion_state']=='published' and row['sha256']==sha
        assert row['original_relative_path']=='fixture.flv'
        output=root/row['relative_path']
        assert hashlib.sha256(output.read_bytes()).hexdigest()==row['stored_sha256']
        assert source.read_bytes()==original
        metadata=db.execute('SELECT metadata FROM video_metadata WHERE media_id=%s',(media['id'],)).fetchone()['metadata'];db.commit()
        assert metadata['conversion']['method']==method
        if validation=='full':
            assert metadata['frame_validation']['source_decoded']==metadata['frame_validation']['output_decoded']
        else:
            assert metadata['conversion_validation']['playback']=='review-required'
        monkeypatch.setattr(videos,'run_tool',lambda *args,**kwargs:pytest.fail('Published resume must not run a decoder'))
        video_converter.convert_one(db,row,config,tools,SimpleNamespace(check=lambda:None))
        row=db.execute('SELECT * FROM media WHERE id=%s',(media['id'],)).fetchone();db.commit()
        assert row['conversion_state']=='done' and source.read_bytes()==original
        video_converter.convert_one(db,row,config,tools,SimpleNamespace(check=lambda:None))


def test_saved_flv_profile_change_is_rejected(tmp_path,monkeypatch):
    from app.video import conversions
    from test_conversions import config
    monkeypatch.setattr(conversions,'DATA',tmp_path)
    conversions.save_parameters(config(),'fixture-library')
    path=tmp_path/'flv-job.json';saved=json.loads(path.read_text())
    saved['profile']['preserve_original']=False;path.write_text(json.dumps(saved))
    with pytest.raises(RuntimeError,match='FLV settings'):
        conversions.save_parameters(config(),'fixture-library')


def test_flv_scope_and_history_are_per_import(env):
    from app.jobs.job_scope import pending_work
    from app.imports.history import history
    _,config,_=env
    with database.connection() as db:
        job=db.execute("INSERT INTO jobs(source_root,state,inventory_complete) VALUES('fixture-import','completed-review-required',true) RETURNING id").fetchone()['id']
        other=db.execute("INSERT INTO jobs(source_root,state,inventory_complete) VALUES('other-import','completed-review-required',true) RETURNING id").fetchone()['id']
        media=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size,first_import_job_id) VALUES(%s,'movie.flv','movie.flv','video',5,%s) RETURNING id",('f'*64,job)).fetchone()['id']
        db.execute("INSERT INTO video_metadata(media_id,state) VALUES(%s,'done')",(media,));db.commit()
        assert pending_work(db,job,'conversion')
        assert not pending_work(db,other,'conversion')
        row=next(r for r in history(db) if r['id']==job)
        assert row['stages']['conversion']=={'pending':1}
        db.execute("UPDATE media SET conversion_state='done',original_relative_path='movie.flv',relative_path='movie.mp4' WHERE id=%s",(media,));db.commit()
        assert not pending_work(db,job,'conversion')
