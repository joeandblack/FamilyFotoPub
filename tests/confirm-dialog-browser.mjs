import {chromePath} from './chrome.mjs';
import {createServer} from 'node:http';
import {readFileSync,mkdtempSync,existsSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(resolve('temp/confirm-dialog-')),profile=join(out,'profile');
const server=createServer((req,res)=>{
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<html data-theme="familyfoto"><link rel="stylesheet" href="/styles.css"><button id="origin">Delete selected</button><script type="module">import {confirmAction} from "/confirm-dialog.mjs"; window.openConfirm=()=>{window.result=null;confirmAction("Permanently delete 15 selected items? This cannot be undone. Destination copies will be removed; source files are not affected.",{title:"Permanently delete photos and videos?",confirmLabel:"Delete permanently",danger:true}).then(value=>window.result=value);};window.ready=true;</script>');return;}
 const name=req.url.slice(1);if(!['styles.css','confirm-dialog.mjs','deletion-progress.mjs'].includes(name)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',name.endsWith('.mjs')?'text/javascript':'text/css');res.end(readFileSync(resolve('web',name)));
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const url=`http://127.0.0.1:${server.address().port}/`;
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,url],{windowsHide:true,stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));let ws;
try{
 for(let n=0;n<100&&!existsSync(join(profile,'DevToolsActivePort'));n++)await sleep(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 const pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let next=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++next;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const run=async expression=>{const r=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description);return r.result.value;};
 for(let n=0;n<100&&!await run('window.ready');n++)await sleep(50);
 await run(`document.body.dataset.view='trash';const tile=document.createElement('button');tile.id='selection-test';tile.className='tile selectable';document.body.append(tile);`);
 assert.equal(await run('getComputedStyle(document.getElementById("selection-test"),"::after").content'),'none');
 await run('document.getElementById("selection-test").classList.add("selected")');
 assert.notEqual(await run('getComputedStyle(document.getElementById("selection-test"),"::after").content'),'none');
 await run('document.getElementById("selection-test").classList.remove("selected");document.body.dataset.view="all"');
 assert.equal(await run('getComputedStyle(document.getElementById("selection-test"),"::after").content'),'none');
 await run('document.getElementById("selection-test").innerHTML=\'<span class="video-duration">1:23</span>\'');
 for(const view of ['all','image','video','trash','album']){
  await run(`document.body.dataset.view=${JSON.stringify(view)};document.getElementById('selection-test').classList.remove('selected')`);
  assert.equal(await run('getComputedStyle(document.getElementById("selection-test"),"::after").content'),'none');
  assert.equal(await run('getComputedStyle(document.querySelector(".video-duration")).right'),'6px');
  await run('document.getElementById("selection-test").classList.add("selected")');
  assert.notEqual(await run('getComputedStyle(document.getElementById("selection-test"),"::after").content'),'none');
  assert.equal(await run('getComputedStyle(document.querySelector(".video-duration")).right'),'36px');
 }
 await run('document.getElementById("selection-test").remove()');
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:800,deviceScaleFactor:1,mobile:false});
  await run('document.getElementById("origin").focus();openConfirm()');
  assert.equal(await run('document.activeElement.textContent'),'Cancel');
  assert.ok(await run('(()=>{const d=document.querySelector("dialog"),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth;})()'));
  writeFileSync(join(out,`confirmation-${width}.png`),Buffer.from((await cdp('Page.captureScreenshot')).data,'base64'));
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await sleep(50);
  assert.equal(await run('window.result'),false);
  assert.equal(await run('document.activeElement.id'),'origin');
 }
 await run('openConfirm();document.querySelector(".confirm-danger").click()');assert.equal(await run('window.result'),true);
 await run('openConfirm();document.querySelector(".confirm-dialog-actions button").click()');assert.equal(await run('window.result'),false);
 await run(`(async()=>{window.showNotice=(await import('/confirm-dialog.mjs')).showNotice;
 openConfirm();window.noticesClosed=0;showNotice('<b>First error</b>').then(()=>noticesClosed++);showNotice('Second error').then(()=>noticesClosed++);})()`);
 assert.equal(await run('document.querySelectorAll(".confirm-dialog").length'),1);
 await run('document.querySelector(".confirm-dialog-actions button").click()');
 assert.equal(await run('document.querySelector("#ff-confirm-message").textContent'),'<b>First error</b>');
 assert.equal(await run('document.querySelector("#ff-confirm-message b")'),null);
 assert.equal(await run('document.querySelectorAll(".confirm-dialog button").length'),1);
 await run('document.querySelector(".confirm-dialog button").click()');
 assert.equal(await run('document.querySelector("#ff-confirm-message").textContent'),'Second error');
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await sleep(30);
 assert.equal(await run('noticesClosed'),2);
 await run(`(async()=>{window.deleteWithProgress=(await import('/deletion-progress.mjs')).deleteWithProgress;
 window.previewImages=await Promise.all(['red','blue','green'].map(async color=>{const img=new Image();img.src='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="'+color+'"/></svg>');await img.decode();return img;}));
 window.startDeletion=()=>{
  window.calls=[];window.pendingDeletes=[];window.deletionResult=null;window.refreshed=false;
  document.querySelectorAll('[data-test-tile]').forEach(n=>n.remove());
  for(const id of [1,2,3]){const n=document.createElement('div');n.dataset.testTile=id;n.textContent='Photo '+id;n.append(previewImages[id-1]);document.body.append(n);}
  deleteWithProgress({ids:[1,2,3],elements:id=>[document.querySelector('[data-test-tile="'+id+'"]')],
   api:url=>{if([...document.querySelectorAll('[data-test-tile]')].some(n=>!n.hidden))throw Error('Tiles not hidden before API request');calls.push(url);return new Promise((resolve,reject)=>pendingDeletes.push({resolve,reject}));},
   refresh:async()=>{window.refreshed=true;}
  }).then(r=>window.deletionResult=r);
 };startDeletion();})()`);
 for(let n=0;n<100&&!await run('calls.length');n++)await sleep(20);
 assert.equal(await run('calls.length'),1);
 assert.deepEqual(await run('[...document.querySelector(".deletion-preview canvas").getContext("2d").getImageData(48,36,1,1).data]'),[255,0,0,255]);
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 assert.equal(await run('document.querySelector(".deletion-progress").open'),true);
 writeFileSync(join(out,'deleting-mobile.png'),Buffer.from((await cdp('Page.captureScreenshot')).data,'base64'));
 await run('pendingDeletes[0].resolve()');
 for(let n=0;n<100&&await run('calls.length')<2;n++)await sleep(20);
 assert.equal(await run('document.querySelector(".deletion-progress progress").value'),1);
 assert.deepEqual(await run('[...document.querySelector(".deletion-preview canvas").getContext("2d").getImageData(48,36,1,1).data]'),[0,0,255,255]);
 await run('pendingDeletes[1].reject(Error("Test disk error"))');await sleep(30);
 assert.equal(await run('calls.length'),2);
 assert.deepEqual(await run('[...document.querySelectorAll("[data-test-tile]")].map(n=>n.hidden)'),[true,false,false]);
 assert.equal(await run('window.refreshed'),true);
 await run('document.querySelector(".deletion-progress button").click()');
 assert.equal(await run('deletionResult.deleted'),1);
 assert.match(await run('deletionResult.error'),/Test disk error/);
 assert.equal(await run('document.querySelector(".deletion-progress")'),null);
 await run('startDeletion()');
 for(let i=0;i<3;i++){
  for(let n=0;n<100&&await run('calls.length')<=i;n++)await sleep(20);
  await run('pendingDeletes['+i+'].resolve()');
 }
 await sleep(30);assert.equal(await run('deletionResult.deleted'),3);
 assert.equal(await run('document.querySelector(".deletion-progress")'),null);
 console.log('Desktop/mobile layout, confirmation, Cancel, Escape and focus restoration passed. '+out);
 await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
