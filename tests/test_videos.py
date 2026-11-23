import json
from datetime import datetime
import pytest
from app.video.metadata import parse_metadata, run_tool, is_sidecar


def test_audio_extensions_use_verified_nonphoto_import():
    from app.library.media import AUDIO,kind_for
    from app.imports.ignore import should_ignore
    for suffix in AUDIO:
        assert kind_for('Music'+suffix.upper())=='video'
        assert not should_ignore('Music'+suffix.upper())


def test_audio_only_mp4_skips_poster(tmp_path,monkeypatch):
    from app.video import metadata as videos
    probe={'streams':[{'codec_type':'audio','codec_name':'aac'}],
           'format':{'duration':'215.9','format_name':'aac'}}
    calls=[]
    def tool(command,*args,**kwargs):
        calls.append(command)
        return json.dumps(probe).encode()
    monkeypatch.setattr(videos,'run_tool',tool)
    metadata,_,error=videos.extract(tmp_path/'misnamed.mp4','a'*64,{'ffprobe':'probe','ffmpeg':'decode'},lambda:None)
    assert metadata['media_type']=='audio' and not metadata['poster'] and error is None
    assert len(calls)==1
    probe['streams'].append({'codec_type':'video','codec_name':'mjpeg','disposition':{'attached_pic':1}})
    assert parse_metadata(probe,'.mp3')[0]['media_type']=='audio'


def test_video_metadata_provenance_and_compatibility():
    probe={'streams':[{'codec_type':'video','codec_name':'h264','width':640,'height':480},
                      {'codec_type':'audio','codec_name':'aac'}],
           'format':{'duration':'2.5','format_name':'mov,mp4','tags':{'creation_time':'2011-05-06T10:20:30Z'}}}
    metadata,date=parse_metadata(probe,'.mov')
    assert date==datetime(2011,5,6,10,20,30)
    assert metadata['capture_time_provenance']=='container.creation_time'
    assert metadata['capture_time_timezone']=='UTC'
    assert 'Likely compatible' in metadata['browser_compatibility']
    metadata,_=parse_metadata(probe,'.mts')
    assert 'external player' in metadata['browser_compatibility']
    probe['format']['duration']='nan'
    assert parse_metadata(probe,'.mov')[0]['duration_seconds'] is None
    with pytest.raises(ValueError,match='No video stream'):
        parse_metadata({'streams':[]},'.avi')


def test_decoder_timeout_requests_review_without_kill(tmp_path,monkeypatch):
    from app.video import metadata as videos
    from app.storage.disks import DiskError
    import subprocess
    class Decoder:
        pid=123
        def communicate(self,timeout): raise subprocess.TimeoutExpired('ffprobe',timeout)
    monkeypatch.setattr(videos,'DATA',tmp_path)
    monkeypatch.setattr(videos.subprocess,'Popen',lambda *a,**kw:Decoder())
    with pytest.raises(DiskError,match='child not killed'):
        run_tool(['ffprobe.exe','fixture.mp4'])
    assert json.loads((tmp_path/'video-decoder-busy.json').read_text())['pid']==123


def test_sidecar_requires_header_and_name():
    assert is_sidecar('._movie.mov',b'\x00\x05\x16\x07')
    assert not is_sidecar('._movie.mov',b'RIFF')
    assert not is_sidecar('movie.mov',b'\x00\x05\x16\x07')


def test_poster_has_no_realtime_throttle(tmp_path, monkeypatch):
    from app.video import metadata as videos
    from PIL import Image
    calls=[]
    def tool(command):
        calls.append(command)
        if command[0]=='probe':
            return json.dumps({'streams':[{'codec_type':'video','width':16,'height':16}],
                               'format':{'duration':'2'}}).encode()
        Image.new('RGB',(16,16)).save(command[-1])
        return b''
    monkeypatch.setattr(videos,'DATA',tmp_path)
    monkeypatch.setattr(videos,'run_tool',tool)
    observed=[]
    result,_,error=videos.extract(tmp_path/'clip.mp4','a'*64,{'ffprobe':'probe','ffmpeg':'decode'},lambda:None,observed.append)
    assert result['poster'] and error is None
    assert observed[0]['duration_seconds']==2
    assert '-readrate' not in calls[1] and '-re' not in calls[1]


def test_unlimited_checksum_still_reads_all_bytes(tmp_path):
    import hashlib
    from app.library.media import digest,TrafficLimit
    path=tmp_path/'clip.bin'
    data=b'video verification fixture'*100
    path.write_bytes(data)
    checks=[]
    assert digest(path,lambda:checks.append(True),TrafficLimit(0))==hashlib.sha256(data).hexdigest()
    assert len(checks)>=2


@pytest.mark.parametrize('mode',['missing','malformed','fallback','storage','missing-tool'])
def test_poster_output_validation_and_fallback(tmp_path,monkeypatch,mode):
    from app.video import metadata as videos
    from PIL import Image
    path=tmp_path/'short.mp4';path.write_bytes(b'fixture')
    calls=[]
    def tool(command):
        if command[0]=='probe':
            return json.dumps({'streams':[{'codec_type':'video','width':16,'height':16}], 'format':{'duration':'0.103'}}).encode()
        calls.append(command)
        if mode=='missing-tool':raise FileNotFoundError('decoder executable missing')
        if mode=='storage':raise PermissionError('disk access denied')
        if mode=='malformed':Path(command[-1]).write_bytes(b'not a jpeg')
        if mode=='fallback' and len(calls)==2:Image.new('RGB',(16,16)).save(command[-1])
        return b''
    from pathlib import Path
    monkeypatch.setattr(videos,'DATA',tmp_path);monkeypatch.setattr(videos,'run_tool',tool)
    if mode in ('storage','missing-tool'):
        with pytest.raises(OSError):videos.extract(path,'b'*64,{'ffprobe':'probe','ffmpeg':'decode'},lambda:None)
        return
    metadata,_,error=videos.extract(path,'b'*64,{'ffprobe':'probe','ffmpeg':'decode'},lambda:None)
    assert len(calls)==2 and '-ignore_editlist' in calls[1] and '-fps_mode' in calls[1]
    assert metadata['duration_seconds']==0.103
    assert metadata['poster']==(mode=='fallback')
    assert (error is None)==(mode=='fallback')
    assert not list(tmp_path.rglob('*.video-partial.jpg'))


def test_poster_missing_output_does_not_mask_missing_source(tmp_path,monkeypatch):
    from app.video import metadata as videos
    monkeypatch.setattr(videos,'DATA',tmp_path)
    monkeypatch.setattr(videos,'run_tool',lambda command:json.dumps({'streams':[{'codec_type':'video'}],'format':{}}).encode())
    with pytest.raises(FileNotFoundError):videos.extract(tmp_path/'missing.mp4','c'*64,{'ffprobe':'probe','ffmpeg':'decode'},lambda:None)
