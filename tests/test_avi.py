import copy
import os
import pytest
from app.video.avi import validate_conversion


def probes():
    source={'format':{'duration':'14.681333'},'streams':[
        {'codec_type':'video','codec_name':'mjpeg','width':320,'height':240,'nb_frames':'440'},
        {'codec_type':'audio','codec_name':'pcm_s16le'}]}
    result=copy.deepcopy(source)
    result['streams'][0]['codec_name']='h264'
    result['streams'][1]['codec_name']='aac'
    return source,result


def test_conversion_accepts_same_resolution_duration_streams():
    validate_conversion(*probes())


@pytest.mark.parametrize('change',['resolution','duration','frames','audio','codec'])
def test_conversion_rejects_changed_content(change):
    source,result=probes()
    if change=='resolution': result['streams'][0]['width']=640
    if change=='duration': result['format']['duration']='10'
    if change=='frames': result['streams'][0]['nb_frames']='400'
    if change=='audio': result['streams'].pop()
    if change=='codec': result['streams'][0]['codec_name']='mjpeg'
    with pytest.raises(ValueError): validate_conversion(source,result)


def test_multiple_streams_require_review():
    source,result=probes();source['streams'].append(source['streams'][1])
    with pytest.raises(ValueError): validate_conversion(source,result)


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires local PostgreSQL and FFmpeg')
@pytest.mark.parametrize('retain_original',[False,True])
def test_publish_before_cleanup_and_resume(tmp_path,monkeypatch,retain_original,sample_avi):
    import hashlib
    import uuid
    from pathlib import Path
    from psycopg import sql
    from app import database, telemetry
    from app.video import avi
    from app.video import metadata as videos
    from app.config import load_config, ROOT
    original=load_config()['database']
    schema='fftest_'+uuid.uuid4().hex
    with database.connection() as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    config={'database':{**original,'options':f'-c search_path={schema}'},
        'destination':{'root':str(tmp_path/'destination')},
        'avi_conversion':{'enabled':True,'remove_destination_avi':True},
        'conversion_performance':{'io_mibps':0,'retained_original_validation':'quick'}}
    (tmp_path/'destination').mkdir()
    data=tmp_path/'data';data.mkdir()
    monkeypatch.setattr(database,'load_config',lambda:config)
    monkeypatch.setattr(avi,'DATA',data)
    monkeypatch.setattr(videos,'DATA',data)
    monkeypatch.setattr(telemetry,'LOGS',tmp_path/'logs')
    payload=sample_avi.read_bytes()
    sha=hashlib.sha256(payload).hexdigest()
    source=tmp_path/'source.avi';source.write_bytes(payload)
    relative=f'{sha[:2]}/{sha[2:4]}/{sha}.avi'
    destination=tmp_path/'destination'/relative
    destination.parent.mkdir(parents=True);destination.write_bytes(payload)
    class Monitor:
        def check(self): pass
    tools=videos.tool_paths()
    commands=[];run_tool=videos.run_tool
    def recorded(command,**kwargs):
        commands.append(command)
        return run_tool(command,**kwargs)
    monkeypatch.setattr(videos,'run_tool',recorded)
    try:
        database.migrate()
        with database.connection() as db:
            media=db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES(%s,%s,'sample.avi','video',%s) RETURNING *",(sha,relative,len(payload))).fetchone()
            db.execute("INSERT INTO video_metadata(media_id,state,metadata) VALUES(%s,'done','{}')",(media['id'],));db.commit()
            if retain_original:
                import json
                hold=data/'conversion-recovery'/str(media['id'])/'retain-original.json'
                hold.parent.mkdir(parents=True)
                hold.write_text(json.dumps({'media_id':media['id'],'sha256':sha}))
                avi.convert_one(db,media,config,tools,Monitor())
                assert any('null' in command for command in commands)
                row=db.execute('SELECT * FROM media WHERE id=%s',(media['id'],)).fetchone()
                assert row['conversion_state']=='done'
                assert destination.read_bytes()==payload and source.read_bytes()==payload
                assert (tmp_path/'destination'/row['relative_path']).is_file()
                return
            unlink=Path.unlink
            def interrupt_cleanup(path,*args,**kwargs):
                if path==destination: raise OSError('Simulated cleanup interruption')
                return unlink(path,*args,**kwargs)
            monkeypatch.setattr(Path,'unlink',interrupt_cleanup)
            with pytest.raises(OSError,match='cleanup interruption'):
                avi.convert_one(db,media,config,tools,Monitor())
            assert any('null' in command for command in commands)
            row=db.execute('SELECT * FROM media WHERE id=%s',(media['id'],)).fetchone();db.commit()
            assert row['conversion_state']=='published'
            assert row['sha256']==sha and row['stored_sha256']!=sha
            assert row['original_relative_path']==relative
            assert destination.exists()
            assert (tmp_path/'destination'/row['relative_path']).is_file()
            monkeypatch.setattr(Path,'unlink',unlink)
            avi.convert_one(db,row,config,tools,Monitor())
            row=db.execute('SELECT * FROM media WHERE id=%s',(media['id'],)).fetchone()
            assert row['conversion_state']=='done'
            assert not destination.exists()
            assert source.read_bytes()==payload
            target=tmp_path/'destination'/row['relative_path']
            assert hashlib.sha256(target.read_bytes()).hexdigest()==row['stored_sha256']
    finally:
        import psycopg
        with psycopg.connect(**original) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
