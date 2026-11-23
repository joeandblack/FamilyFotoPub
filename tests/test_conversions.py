import json
import pytest
from app.video import conversions


def config():
    return {'destination':{'root':'E:/FamilyFoto/media','serial':'disk','volume_serial':'vol','volume_id':'id'},
            'database':{'host':'localhost','dbname':'test','password':'secret'},
            'avi_conversion':{'enabled':True,'remove_destination_avi':True}}


def test_parameters_durable_and_source_independent(tmp_path,monkeypatch):
    monkeypatch.setattr(conversions,'DATA',tmp_path)
    c=config()
    saved=conversions.save_parameters(c,'library')
    assert 'secret' not in json.dumps(saved)
    c['source']={'root':'F:/PHOTOS/OTHER'}
    assert conversions.save_parameters(c,'library')==saved
    c['avi_conversion']['remove_destination_avi']=False
    with pytest.raises(RuntimeError,match='changed'):
        conversions.save_parameters(c,'library')


def test_resume_rejects_other_library(tmp_path,monkeypatch):
    monkeypatch.setattr(conversions,'DATA',tmp_path)
    conversions.save_parameters(config(),'one')
    with pytest.raises(RuntimeError,match='changed'):
        conversions.save_parameters(config(),'two')


def test_decoder_lease_blocks_before_database(tmp_path,monkeypatch):
    monkeypatch.setattr(conversions,'DATA',tmp_path)
    (tmp_path/'video-decoder-busy.json').write_text('{}')
    with pytest.raises(RuntimeError,match='Decoder'):
        conversions.check_ready(None)
