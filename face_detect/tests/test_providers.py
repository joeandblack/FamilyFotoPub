import copy
import hashlib
import json
import pytest
from PIL import Image
from face_detect.configuration import resolve,read_manifest
from face_detect.contracts import ConfigurationError,ProviderUnavailable,validate_faces
from face_detect.service import FaceService


def face():
    return {'box':dict(x_min=10,y_min=10,x_max=90,y_max=90,probability=.99),'landmarks':[[25,30],[75,30],[50,50],[30,70],[70,70]],'vector':[2.]+[0.]*511}

@pytest.fixture
def model(tmp_path):
    assets={}
    for name in ('detector','recognizer'):
        p=tmp_path/name;p.write_bytes(name.encode());assets[name]={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    manifest={'schema':'familyfoto-native-model-v1','provider':'insightface','dimensions':512,'normalization':'l2','license':{'approved':True,'source':'test'},'assets':assets,'matching':{'mode':'review-only'}}
    p=tmp_path/'manifest.json';p.write_text(json.dumps(manifest))
    return {'provider':'insightface','device':'cpu','model_manifest':str(p)},manifest


def test_platform_default_and_explicit_override():
    profiles={'mac':{'provider':'insightface'},'alternate':{'provider':'facenet'}}
    cfg={'face_detection':{'platform_defaults':{'darwin':'mac'},'profiles':profiles}}
    assert resolve(cfg,'darwin')[0]=='mac'
    cfg['face_detection']['active_profile']='alternate'
    assert resolve(cfg,'darwin')[1]['provider']=='facenet'
    cfg['face_detection']['active_profile']='typo'
    with pytest.raises(ConfigurationError):resolve(cfg,'darwin')


def test_local_assets_fail_closed(model,tmp_path):
    profile,manifest=model
    _,assets=read_manifest(profile,tmp_path)
    assets['detector'].write_bytes(b'changed')
    with pytest.raises(ConfigurationError,match='checksum'):read_manifest(profile,tmp_path)


def test_wrapper_normalizes_and_pins_model(model,tmp_path):
    profile,manifest=model;_,assets=read_manifest(profile,tmp_path)
    class Backend:
        def analyze(self,image,include_embeddings=True):return [face()]
        def close(self):pass
    service=FaceService(profile,manifest,assets,Backend())
    assert service.detect(Image.new('RGB',(100,100)))[0]['vector'][0]==1
    other=FaceService(profile,manifest|{'matching':{'calibrated':True,'threshold':.7,'margin':.1}},assets,Backend())
    assert service.identity!=other.identity
    with pytest.raises(ProviderUnavailable):service.validate({'model_id':other.identity})
    assets['recognizer'].write_bytes(b'changed')
    with pytest.raises(ProviderUnavailable):service.observe()

@pytest.mark.parametrize('bad',['nan','zero','dimension','bounds','score'])
def test_invalid_backend_results_rejected(bad):
    f=face()
    if bad=='nan':f['vector'][0]=float('nan')
    if bad=='zero':f['vector']=[0.]*512
    if bad=='dimension':f['vector']=[1.]
    if bad=='bounds':f['box']['x_max']=101
    if bad=='score':f['box']['probability']=2
    with pytest.raises(ProviderUnavailable):validate_faces([f],(100,100),512)


def test_legacy_gateway_never_auto_launches(tmp_path,monkeypatch):
    from face_detect.api import people_preview_gateway as g
    p=tmp_path/'settings.json';p.write_text(json.dumps({'enabled':True,'pilot':'a'*32,'port':54341}))
    monkeypatch.setattr(g,'SETTINGS',p)
    assert g.settings() is None
