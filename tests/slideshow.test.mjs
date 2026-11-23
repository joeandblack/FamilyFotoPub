import test from 'node:test';
import assert from 'node:assert/strict';
import {createSlideshow} from '../web/slideshow.mjs';

function fixture(){
  let task=null,delay=0,advances=0,errors=[];
  const show=createSlideshow({advance:()=>advances++,changed:()=>{},error:e=>errors.push(e),
    schedule:(fn,ms)=>{task=fn;delay=ms;return 1;},cancel:()=>{task=null;}});
  function media(tag){const m=new EventTarget();Object.assign(m,{tagName:tag,complete:false,naturalWidth:0,play:()=>Promise.resolve(),pause:()=>m.dispatchEvent(new Event('pause'))});return m;}
  return {show,media,get task(){return task;},get delay(){return delay;},get advances(){return advances;},errors};
}
test('photo timer waits for load, restarts on navigation and interval change',()=>{
  const f=fixture(),a=f.media('IMG');f.show.attach(a);f.show.toggle();assert.equal(f.task,null);
  a.complete=true;a.naturalWidth=100;a.dispatchEvent(new Event('load'));assert.equal(f.delay,3000);
  f.show.interval(10);assert.equal(f.delay,10000);f.task();assert.equal(f.advances,1);
  f.show.attach(f.media('IMG'));assert.equal(f.task,null);
  a.dispatchEvent(new Event('load'));assert.equal(f.task,null);
});
test('mixed sequence waits for video end, manual pause stops autoplay',()=>{
  const f=fixture(),v=f.media('VIDEO');f.show.attach(v);f.show.toggle();assert.equal(f.task,null);
  v.ended=true;v.dispatchEvent(new Event('ended'));assert.equal(f.advances,1);
  const image=f.media('IMG');image.complete=true;image.naturalWidth=100;f.show.attach(image);assert.equal(f.delay,3000);
  f.show.attach(v);v.ended=false;v.pause();assert.equal(f.show.active,false);
});
test('pause and close cancel timers and detach ended listeners',()=>{
  const f=fixture(),a=f.media('IMG');a.complete=true;a.naturalWidth=100;f.show.attach(a);f.show.toggle();
  f.show.toggle();assert.equal(f.task,null);f.show.toggle();f.show.reset();assert.equal(f.task,null);
  a.dispatchEvent(new Event('ended'));assert.equal(f.advances,0);assert.equal(f.show.active,false);
});
test('playback rejection stops with a message; old playback rejections are ignored',async()=>{
  const f=fixture(),v=f.media('VIDEO');v.play=()=>Promise.reject(Error('blocked'));f.show.attach(v);f.show.toggle();
  await Promise.resolve();assert.equal(f.show.active,false);assert.equal(f.errors.length,1);
  f.show.toggle();f.show.attach(f.media('IMG'));await Promise.resolve();assert.equal(f.show.active,true);assert.equal(f.errors.length,1);
});
test('preview errors stop instead of skipping files',()=>{
  const f=fixture(),a=f.media('IMG');f.show.attach(a);f.show.toggle();a.dispatchEvent(new Event('error'));
  assert.equal(f.show.active,false);assert.equal(f.errors.length,1);assert.equal(f.advances,0);
});
