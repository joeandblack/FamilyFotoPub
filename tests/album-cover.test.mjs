import test from 'node:test';
import assert from 'node:assert/strict';
import {coverFraming,coverGeometry} from '../web/album-cover.mjs';
test('framing belongs to the current cover only',()=>{
 const stored={media_id:4,mode:'fit',zoom:2,x:.5,y:-.2};
 assert.equal(coverFraming({cover_id:4,cover_framing:stored}).zoom,2);
 assert.deepEqual(coverFraming({cover_id:5,cover_framing:stored}),{mode:'fill',zoom:1,x:0,y:0});
});
test('fill crops, fit contains, and pan stays within the image or its letterbox',()=>{
 const settings={mode:'fill',zoom:1,x:0,y:0};
 const fill=coverGeometry(600,1200,settings);assert.equal(fill.width,100);assert.ok(Math.abs(fill.height-800/3)<1e-10);
 const fit=coverGeometry(600,1200,{...settings,mode:'fit'});assert.equal(fit.height,100);assert.equal(fit.width,37.5);
 const edge=coverGeometry(600,1200,{...settings,y:1});assert.ok(Math.abs(edge.top-edge.height/2)<1e-10);
 const zoomed=coverGeometry(800,600,{...settings,zoom:2});assert.ok(Math.abs(zoomed.width-200)<1e-10);assert.ok(Math.abs(zoomed.height-200)<1e-10);
});
