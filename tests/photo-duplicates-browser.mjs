import {chromePath} from './chrome.mjs';
// Isolated browser fixture: synthetic groups only, never production media.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {join,resolve,extname} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=resolve('.'),out=mkdtempSync(join(root,'temp','photo-duplicates-browser-'));
const html=`<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/web/styles.css"></head><body data-role="admin"><button id="review">Review matches</button><script type="module">
import {setupPhotoDuplicateReview} from '/web/photo-duplicates.mjs';
window.commands=[];let decision='candidate',revision=0,preferred_id=1,restored=false;
window.resetTrash=()=>{restored=false;};window.deleteCalls=0;window.deletedIds=[];window.trashCount=1;window.failDelete=false;
const members=[1,2].map(id=>({id,name:id===1?'IMG_0876.JPG':'20150906_034329422_iOS.jpg',size:123456,captured_at:'2015-09-06',evidence:{camera:'Fixture camera',orientation:1,exif_sha256:'1234567890abcdef',gps:''}}));
const open=setupPhotoDuplicateReview(async(url,options)=>{
 if(url.endsWith('/restore')){restored=true;return {ok:true};}
 if(url.endsWith('/trash/delete')){window.deleteCalls++;if(window.failDelete&&window.deleteCalls===2)throw Error('Fixture safety failure');window.deletedIds.push(Number(url.split('/')[3]));restored=true;return {ok:true};}
 if(url.includes('/trashed?'))return {items:restored?[]:Array.from({length:window.trashCount},(_,i)=>({id:2+i,name:'Duplicate.jpg',preferred_id:1,preferred_name:'Kept.jpg'})),total:restored?0:window.trashCount,pages:1};
 if(options){const command=JSON.parse(options.body);window.commands.push(command);decision=command.decision;preferred_id=command.preferred_id;revision++;return {ok:true};}
 return {groups:[1,2,3,4].map(n=>({key:'fixture-'+n,members,decision,preferred_id,revision,method:'Normalized pixels (same color profile)'})),total:4,pages:1,warnings:[]};
});document.getElementById('review').onclick=()=>open(1);
</script></body></html>`;
const server=createServer((req,res)=>{
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(html);}
 else if(req.url.startsWith('/web/')&&!req.url.includes('..')){res.setHeader('Content-Type',extname(req.url)==='.css'?'text/css':'text/javascript');res.end(readFileSync(join(root,req.url)));}
 else{res.setHeader('Content-Type','image/png');res.end(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64'));}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const url=`http://127.0.0.1:${server.address().port}/`,profile=join(out,'profile');
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1280,900',url],{windowsHide:true,stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));let ws;
try{
 for(let n=0;n<150&&!existsSync(join(profile,'DevToolsActivePort'));n++)await sleep(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 const pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 ws=new WebSocket(pages.find(p=>p.url===url).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0;const pending=new Map();ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}};
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const key=++id;pending.set(key,{resolve,reject});ws.send(JSON.stringify({id:key,method,params}));});
 const run=async expression=>{const result=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw Error(result.exceptionDetails.text);return result.result.value;};
 const wait=async expression=>{for(let n=0;n<100;n++){if(await run(expression))return;await sleep(50);}throw Error('Timeout '+expression);};
 await wait("!!document.getElementById('review')?.onclick");
 await run("document.getElementById('review').click()");await wait("document.querySelector('.duplicate-review').open");
 assert.equal(await run("document.querySelectorAll('.duplicate-members figure').length"),8);
 await run("document.querySelector('.duplicate-review select').value='2';document.querySelector('.write-action').click()");
 await wait("document.querySelector('.duplicate-review h3').textContent.includes('Confirmed')");
 assert.deepEqual(await run('window.commands[0].member_ids'),[1,2]);
 assert.equal(await run('window.commands[0].preferred_id'),2);
 await run("document.querySelectorAll('.write-action')[1].click()");
 await wait("document.querySelector('.duplicate-review h3').textContent.includes('Keep separate')");
 await run("document.querySelectorAll('.write-action')[2].click()");
 await wait("document.querySelector('.duplicate-review h3').textContent.includes('To review')");
 for(const [width,height] of [[1280,900],[390,844]]){
   await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
   assert(await run("(()=>{const d=document.querySelector('.duplicate-review');return d.scrollWidth<=d.clientWidth+1})()"));
   const positions=await run("[...document.querySelectorAll('.duplicate-review-group')].map(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y}})");
   if(width>760){assert.equal(positions[0].y,positions[1].y);assert(positions[1].x>positions[0].x);}
   else{assert.equal(positions[0].x,positions[1].x);assert(positions[1].y>positions[0].y);}
   assert(await run("[...document.querySelectorAll('.duplicate-review-columns,.duplicate-review-group')].every(e=>getComputedStyle(e).overflowY==='visible')"));
   const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`review-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await run("[...document.querySelectorAll('.duplicate-review-tabs button')].find(b=>b.textContent==='Trashed duplicates').click()");
 await wait("!!document.querySelector('.duplicate-trashed-image')");
 assert.equal(await run("document.querySelectorAll('.duplicate-trash-pair img').length"),2);
 assert(await run("(()=>{const d=document.querySelector('.duplicate-review');return d.scrollWidth<=d.clientWidth+1})()"));
 for(const [width,height] of [[1280,900],[390,844]]){
   await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
   const positions=await run("[...document.querySelectorAll('.duplicate-trash-pair figure')].map(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y}})");
   assert.equal(positions[0].y,positions[1].y);assert(positions[1].x>positions[0].x);
   const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`trash-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await run("window.confirm=()=>false;[...document.querySelectorAll('.duplicate-review button')].find(b=>b.textContent==='Permanently delete').click()");
 assert.equal(await run('window.deleteCalls'),0);
 await run("[...document.querySelectorAll('.duplicate-review button')].find(b=>b.textContent==='Restore').click()");
 await wait("document.querySelector('.duplicate-review').textContent.includes('No trashed duplicates.')");
 await run("document.querySelector('.duplicate-review').close();window.resetTrash();document.getElementById('review').click()");
 await wait("document.querySelector('.duplicate-review').open");
 await run("[...document.querySelectorAll('.duplicate-review-tabs button')].find(b=>b.textContent==='Trashed duplicates').click()");
 await wait("!!document.querySelector('.duplicate-trashed-image')");
 await run("window.confirm=()=>true;[...document.querySelectorAll('.duplicate-review button')].find(b=>b.textContent==='Permanently delete').click()");
 await wait("document.querySelector('.duplicate-review').textContent.includes('No trashed duplicates.')");
 assert.equal(await run('window.deleteCalls'),1);
 const reopenTrash=async()=>{
   await run("document.querySelector('.duplicate-review').close();window.resetTrash();document.getElementById('review').click()");
   await wait("document.querySelector('.duplicate-review').open");
   await run("[...document.querySelectorAll('.duplicate-review-tabs button')].find(b=>b.textContent==='Trashed duplicates').click()");
   await wait("!!document.querySelector('.duplicate-trashed-image')");
 };
 await run('window.trashCount=3;window.deleteCalls=0;window.deletedIds=[]');
 await reopenTrash();
 assert(await run("[...document.querySelectorAll('button')].find(b=>b.textContent.startsWith('Permanently delete selected')).disabled"));
 await run("document.querySelector('input[aria-label=\"Select all trashed photos on this page\"]').click()");
 await run("window.confirm=message=>{window.confirmation=message;return false};[...document.querySelectorAll('button')].find(b=>b.textContent.startsWith('Permanently delete selected')).click()");
 assert.equal(await run('window.deleteCalls'),0);
 assert.match(await run('window.confirmation'),/3 selected/);
 assert.match(await run('window.confirmation'),/cannot be undone/);
 await run("window.confirm=()=>true;[...document.querySelectorAll('button')].find(b=>b.textContent.startsWith('Permanently delete selected')).click()");
 await wait("document.querySelector('.duplicate-review').textContent.includes('No trashed duplicates.')");
 assert.deepEqual(await run('window.deletedIds'),[2,3,4]);
 await run('window.deleteCalls=0;window.deletedIds=[];window.failDelete=true;window.alert=message=>window.lastAlert=message');
 await reopenTrash();
 await run("document.querySelector('input[aria-label=\"Select all trashed photos on this page\"]').click();[...document.querySelectorAll('button')].find(b=>b.textContent.startsWith('Permanently delete selected')).click()");
 await wait('!!window.lastAlert');
 assert.equal(await run('window.deleteCalls'),2);
 assert.deepEqual(await run('window.deletedIds'),[2]);
 assert.match(await run('window.lastAlert'),/Deleted 1 of 3/);
 await wait("document.querySelector('.duplicate-review').textContent.includes('No trashed duplicates.')");
 await run("document.querySelector('.duplicate-review').close();window.resetTrash();document.body.dataset.role='user';document.getElementById('review').click()");
 await wait("document.querySelector('.duplicate-review').open");
 await run("[...document.querySelectorAll('.duplicate-review-tabs button')].find(b=>b.textContent==='Trashed duplicates').click()");
 await wait("!!document.querySelector('.duplicate-trashed-image')");
 assert.equal(await run("[...document.querySelectorAll('.duplicate-review button')].some(b=>b.textContent==='Permanently delete')"),false);
 console.log(JSON.stringify({ok:true,checks:['two-column review','preferred copy','confirm/separate/reset','member snapshot','desktop/mobile overflow','trashed review and restore'],artifacts:out}));
 await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
