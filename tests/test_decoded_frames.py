import os
from unittest.mock import Mock
import pytest
from app.video import avi
from app.video import metadata as videos
from test_avi import probes


def test_header_anomaly_uses_equal_decoded_counts_only():
    before,after=probes();before['streams'][0]['nb_frames']='516'
    assert avi.validate_conversion(before,after,lambda:(440,440))['source_decoded']==440
    for counts in [(440,439),(0,0),(None,None),('440','440')]:
        with pytest.raises(ValueError):avi.validate_conversion(before,after,lambda:counts)
    def failure():raise ValueError('corrupt decode')
    with pytest.raises(ValueError,match='corrupt'):avi.validate_conversion(before,after,failure)
    after['format']['duration']='5'
    decode=Mock()
    with pytest.raises(ValueError):avi.validate_conversion(before,after,decode)
    decode.assert_not_called()


@pytest.mark.parametrize('output',[b'frame=10\nprogress=continue\n',b'frame=0\nprogress=end\n',b'nb_read_packets=10\nprogress=end\n',b'frame=x\nprogress=end\n',b'frame=20\nprogress=continue\nframe=10\nprogress=end\n'])
def test_incomplete_invalid_and_packet_counts_rejected(monkeypatch,output):
    monkeypatch.setattr(videos,'run_tool',lambda *a,**kw:output)
    with pytest.raises(ValueError):avi.decoded_video_frames('fixture',{'ffmpeg':'ffmpeg'},lambda:None)


def test_strict_passthrough_bounded_decode_command(monkeypatch):
    run=Mock(return_value=b'frame=10\nprogress=continue\nframe=20\nprogress=end\n')
    monkeypatch.setattr(videos,'run_tool',run);guard=Mock()
    assert avi.decoded_video_frames('fixture',{'ffmpeg':'ffmpeg'},guard)==20
    args=run.call_args.args[0]
    for flag,value in [('-fps_mode','passthrough'),('-enc_time_base','demux'),('-threads','2'),('-max_error_rate','0'),('-err_detect','explode'),('-progress','pipe:1')]:assert args[args.index(flag)+1]==value
    assert '-xerror' in args and run.call_args.kwargs=={'timeout':3600,'reject_stderr':True}
    assert guard.call_count==2


def test_success_exit_with_error_diagnostics_is_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(videos,'DATA',tmp_path)
    process=Mock(pid=99,returncode=0);process.communicate.return_value=(b'frame=1\nprogress=end\n',b'decode error')
    monkeypatch.setattr(videos.subprocess,'Popen',lambda *a,**k:process)
    with pytest.raises(ValueError,match='decode error'):videos.run_tool(['fixture'],reject_stderr=True)


def test_decode_timeout_preserves_review_lease(tmp_path,monkeypatch):
    import json
    import subprocess
    from app.storage.disks import DiskError
    monkeypatch.setattr(videos,'DATA',tmp_path)
    process=Mock(pid=99);process.communicate.side_effect=subprocess.TimeoutExpired('fixture',3600)
    monkeypatch.setattr(videos.subprocess,'Popen',lambda *a,**k:process)
    with pytest.raises(DiskError,match='safe pause'):avi.decoded_video_frames('fixture',{'ffmpeg':'fixture'},lambda:None)
    assert json.loads((tmp_path/'video-decoder-busy.json').read_text())['state']=='review-required'
    process.kill.assert_not_called()


@pytest.mark.skipif(os.environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Requires installed FFmpeg')
def test_actual_decode_and_corrupt_input(tmp_path,monkeypatch,sample_avi):
    from app.config import ROOT
    monkeypatch.setattr(videos,'DATA',tmp_path)
    path=sample_avi
    count=avi.decoded_video_frames(path,videos.tool_paths(),lambda:None)
    assert count>0
    bad=tmp_path/'bad.avi';bad.write_bytes(b'not a video')
    with pytest.raises(ValueError):avi.decoded_video_frames(bad,videos.tool_paths(),lambda:None)
