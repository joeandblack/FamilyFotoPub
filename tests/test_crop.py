import pytest
from app.photos.crop import validate_crop,pixel_box

def test_valid_and_reset():
    assert validate_crop(None) is None
    c={'x':.1,'y':.2,'width':.5,'height':.6}
    assert validate_crop(c)==c
    assert pixel_box(c,100,100)==(10,20,60,80)

@pytest.mark.parametrize('value',[{},[],{'x':0,'y':0,'width':0,'height':1},
    {'x':-.1,'y':0,'width':1,'height':1},{'x':.5,'y':0,'width':1,'height':1},
    {'x':0,'y':0,'width':float('nan'),'height':1},{'x':True,'y':0,'width':1,'height':1},
    {'x':0,'y':0,'width':'1','height':1},{'x':1,'y':0,'width':1e-10,'height':1}])
def test_invalid_crop(value):
    with pytest.raises(ValueError):validate_crop(value)

def test_tiny_crop_has_at_least_one_pixel():
    assert pixel_box({'x':.99,'y':.99,'width':.01,'height':.01},10,10)==(9,9,10,10)
