import test from 'node:test';
import assert from 'node:assert/strict';
import {setupFaceService} from '../face_detect/web/face-service.mjs';

function fixture(api){
 const originalDocument=globalThis.document,originalInterval=globalThis.setInterval;
 const message={},auto={},dot={},buttons=['start','stop'].map(action=>({dataset:{service:action}}));
 const section={querySelector:selector=>selector==='input'?auto:selector==='.dot'?dot:message,querySelectorAll:()=>buttons};
 const storage={hidden:false,prepend:()=>{}};let poll;
 globalThis.document={body:{dataset:{role:'admin'}},hidden:false,createElement:()=>section,getElementById:()=>storage};
 globalThis.setInterval=callback=>{poll=callback;};
 const controller=setupFaceService({api});
 return {controller,message,auto,dot,buttons,section,storage,poll:()=>poll(),
  restore:()=>{globalThis.document=originalDocument;globalThis.setInterval=originalInterval;}};
}
const ready={state:'ready',message:'Ready',busy:false,start_with_app:false};

test('Settings saves JSON preferences and shows a steady green light during health checks',async()=>{
 const calls=[];let busy=false;
 const f=fixture(async(url,options)=>{
  calls.push({url,options});
  if(url.endsWith('/settings'))return JSON.parse(options.body);
  return options?{state:'starting',message:'Starting...',busy:true}:{...ready,busy};
 });
 try{
  await f.controller.load();
  assert.equal(f.message.textContent,'Ready');assert.equal(f.dot.className,'dot ready');
  assert.ok(!f.section.innerHTML.includes('<progress'));
  assert.equal(f.buttons[0].disabled,true);assert.equal(f.buttons[1].disabled,false);
  busy=true;await f.controller.load();assert.equal(f.dot.className,'dot ready');
  f.auto.checked=true;await f.auto.onchange();
  assert.equal(calls.at(-1).options.headers['Content-Type'],'application/json');
  assert.equal(JSON.parse(calls.at(-1).options.body).start_with_app,true);
  assert.equal(f.auto.checked,true);
  await f.buttons[0].onclick();
  assert.equal(calls.at(-1).url,'/api/face-service/start');assert.equal(f.dot.className,'dot running');
  assert.equal(f.auto.checked,true);
  const count=calls.length;f.storage.hidden=true;f.poll();assert.equal(calls.length,count);
  assert.ok(!calls.some(call=>call.url.includes('jobs')));
 }finally{f.restore();}
});

for(const duringSave of [false,true])test(`stale status cannot undo a saved preference (during save: ${duringSave})`,async()=>{
 let resolveStatus,resolveSave;
 const f=fixture((url)=>url.endsWith('/settings')?new Promise(resolve=>{resolveSave=resolve;}):new Promise(resolve=>{resolveStatus=resolve;}));
 try{
  let load;
  if(!duringSave)load=f.controller.load();
  f.auto.checked=true;const save=f.auto.onchange();
  if(duringSave)load=f.controller.load();
  resolveSave({start_with_app:true});await save;
  resolveStatus(ready);await load;
  assert.equal(f.auto.checked,true);assert.equal(f.auto.disabled,false);
 }finally{f.restore();}
});

test('failed preference save restores checkbox and reports the error',async()=>{
 const f=fixture(async()=>{throw Error('Cannot save settings');});
 try{
  f.auto.checked=true;await f.auto.onchange();
  assert.equal(f.auto.checked,false);assert.equal(f.auto.disabled,false);
  assert.equal(f.message.textContent,'Cannot save settings');
 }finally{f.restore();}
});
