import {chromePath} from './chrome.mjs';
// Disposable browser fixture: no production API or media mutations.
import {createServer} from 'node:http';
import {readFileSync,mkdtempSync,existsSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(resolve('temp/conversion-review-')),profile=join(out,'profile');
const server=createServer((req,res)=>{
 if(req.url==='/'){
  res.setHeader('Content-Type','text/html');res.end(`<html data-theme="familyfoto"><link rel="stylesheet" href="/styles.css"><script type="module">
  import {setupConversionReview} from '/conversion-review.mjs';
  window.writes=[];
  window.review=setupConversionReview({api:async(url,options)=>{if(options?.method==='POST'){window.writes.push(url);await new Promise(r=>setTimeout(r,300));return {ok:true};}return {pages:1,items:Array.from({length:105},(_,i)=>({id:i+1,kind:'video',conversion_state:'done',original_relative_path:'original.mts',name:'Video '+(i+1)+'.MTS',video_metadata:{conversion_validation:{playback:'review-required'}}}))};},openViewer:(id)=>window.played=id});window.ready=true;
  </script>`);return;
 }
 const name=req.url.slice(1);if(!['styles.css','conversion-review.mjs','confirm-dialog.mjs'].includes(name)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',name.endsWith('.mjs')?'text/javascript':'text/css');res.end(readFileSync(resolve('web',name)));
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,`http://127.0.0.1:${server.address().port}/`],{windowsHide:true,stdio:'ignore'});
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
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:800,deviceScaleFactor:1,mobile:false});await run('review({id:1})');
  assert.equal(await run('document.querySelectorAll(".tile").length'),100);
  assert.ok(await run('(()=>{const d=document.querySelector("dialog"),r=d.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&d.scrollWidth<=d.clientWidth;})()'));
  assert.ok(await run('Array.from(document.querySelectorAll("button")).every(b=>b.scrollWidth<=b.clientWidth)'));
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Next").click()');
  assert.equal(await run('document.querySelectorAll(".tile").length'),5);
  await run('document.querySelector(".tile").click()');assert.equal(await run('window.played'),101);
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Select").click();document.querySelector(".tile").click()');
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Done selecting").click()');
  assert.equal(await run('document.querySelectorAll(".tile.selected").length'),0);
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Select").click();document.querySelector(".tile").click()');
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Delete retained originals").click()');
  assert.equal(await run('document.querySelectorAll("dialog[open]").length'),2);
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Cancel").click()');await sleep(20);
  assert.equal(await run('writes.length'),width===1280?0:1);
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Delete retained originals").click()');
  await run('Array.from(document.querySelectorAll("button")).find(b=>b.textContent==="Delete originals").click()');await sleep(20);
  assert.equal(await run('document.querySelector(".conversion-delete-progress").hidden'),false);
  await sleep(350);
  assert.equal(await run('writes.at(-1)'),'/api/import/1/playback-review/101/delete');
  assert.equal(await run('document.querySelectorAll(".tile").length'),4);
  const layout=await run('(()=>{const d=document.querySelector("dialog"),g=d.querySelector(".grid");return {height:d.getBoundingClientRect().height,grid:g.getBoundingClientRect().height,scroll:g.scrollHeight,width:innerWidth};})()');
  assert.ok(layout.height<650,JSON.stringify(layout));
  writeFileSync(join(out,`review-${width}.png`),Buffer.from((await cdp('Page.captureScreenshot')).data,'base64'));
  await run('document.querySelector("dialog").close()');
 }
 console.log('Desktop/mobile review gallery, pagination and playback callbacks passed:',out);
}finally{ws?.close();chrome.kill();await new Promise(r=>server.close(r));}
