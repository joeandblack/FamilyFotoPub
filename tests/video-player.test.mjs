import test from 'node:test';
import assert from 'node:assert/strict';
import {playbackTime,setupFullscreen} from '../web/video-player.mjs';
import {photoLayout} from '../web/crop.mjs';
test('video times handle unknown duration and hour-long playback',()=>{
  assert.equal(playbackTime(NaN),'0:00');assert.equal(playbackTime(Infinity),'0:00');
  assert.equal(playbackTime(65.9),'1:05');assert.equal(playbackTime(3661),'1:01:01');
});
test('rotated video fits desktop and mobile without changing aspect ratio',()=>{
  for(const [w,h] of [[1440,900],[390,844]])for(const rotation of [0,90,180,270]){
    const {frame}=photoLayout(1920,1080,w,h,null,rotation),vw=parseFloat(frame.width),vh=parseFloat(frame.height);
    assert.ok((rotation%180?vh:vw)<=w+0.01);assert.ok((rotation%180?vw:vh)<=h+0.01);
    assert.ok(Math.abs(vw/vh-1920/1080)<0.001);
  }
});

function fullscreenFixture(){
  const doc=new EventTarget();doc.fullscreenEnabled=true;
  doc.documentElement={requestFullscreen:async()=>{doc.fullscreenElement=doc.documentElement;doc.dispatchEvent(new Event('fullscreenchange'));}};
  doc.exitFullscreen=async()=>{doc.fullscreenElement=null;doc.dispatchEvent(new Event('fullscreenchange'));};
  const button={setAttribute(name,value){this[name]=value;}};
  return {doc,button};
}
test('fullscreen uses viewer surface, toggles label and exits on disposal',async()=>{
  const {doc,button}=fullscreenFixture();
  const surface={requestFullscreen:async()=>{doc.fullscreenElement=surface;doc.dispatchEvent(new Event('fullscreenchange'));}};
  const dispose=setupFullscreen(button,doc,assert.fail,undefined,surface);
  await button.onclick();assert.equal(doc.fullscreenElement,surface);
  assert.equal(button.title,'Exit fullscreen');
  await button.onclick();assert.equal(doc.fullscreenElement,null);
  assert.equal(button.title,'Fullscreen');
  await button.onclick();dispose();assert.equal(doc.fullscreenElement,null);
});
test('fullscreen rejection is visible and unavailable API hides button',async()=>{
  const {doc,button}=fullscreenFixture();let error;
  doc.documentElement.requestFullscreen=async()=>{throw Error('Permission denied');};
  const dispose=setupFullscreen(button,doc,message=>error=message,undefined,doc.documentElement);
  await button.onclick();assert.match(error,/Permission denied/);dispose();
  doc.fullscreenEnabled=false;setupFullscreen(button,doc,assert.fail,undefined,doc.documentElement)();assert.equal(button.hidden,true);
});
test('disposal leaves preexisting fullscreen alone',()=>{
  const {doc,button}=fullscreenFixture();doc.fullscreenElement=doc.documentElement;
  setupFullscreen(button,doc,assert.fail,undefined,doc.documentElement)();assert.equal(doc.fullscreenElement,doc.documentElement);
});
