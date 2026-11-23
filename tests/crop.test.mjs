import test from 'node:test';
import assert from 'node:assert/strict';
import {rotateCrop,photoLayout} from '../web/crop.mjs';
test('crop coordinates round trip through every rotation',()=>{
  const crop={x:.1,y:.2,width:.3,height:.4};
  for(const angle of [0,90,180,270]){
    const restored=rotateCrop(rotateCrop(crop,angle),-angle);
    for(const key in crop)assert.ok(Math.abs(crop[key]-restored[key])<1e-9);
  }
});
test('cropped viewer fits before and after rotation',()=>{
  const crop={x:.25,y:.25,width:.5,height:.5};
  assert.deepEqual(photoLayout(1200,800,600,400,crop,0),{
    frame:{width:'600px',height:'400px',transform:'rotate(0deg)'},
    image:{width:'1200px',height:'800px',left:'-300px',top:'-200px'}});
  const rotated=photoLayout(1200,800,400,600,crop,90);
  assert.equal(rotated.frame.width,'600px');assert.equal(rotated.frame.height,'400px');
});
