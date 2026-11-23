import {chromePath} from './chrome.mjs';
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(resolve('temp/import-sections-')),sleep=ms=>new Promise(r=>setTimeout(r,ms));
const server=createServer((req,res)=>{
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(`<meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/web/styles.css"><script>window.errors=[];addEventListener('error',e=>errors.push(e.message));addEventListener('unhandledrejection',e=>errors.push(String(e.reason)));</script><main><h1>Imports</h1><section id="manualImport"><details><summary>Select source media files to import</summary><input value="F:/Photos" id="source"></details></section><div id="history"></div></main><script type="module">
 import {renderPipelines,updateImportPeopleStatus} from '/web/import-pipelines.mjs';
 window.updatePeople=status=>updateImportPeopleStatus(document.getElementById('history'),status);
 const batch=id=>({id,source_root:'F:/Photos'+id,started_at:'2026-10-28T00:00:00Z',state:'completed-review-required',total:2,total_bytes:100,counts:{imported:2},stages:{}});
 window.draw=(current=false)=>{const items=[batch(24),batch(22)];if(current){items[0].state='scanning';items[0].counts={pending:2};}renderPipelines(document.getElementById('history'),items,current?{busy:true,job_id:24,stage:'import'}:{},{command(){},options(){},peopleReview:{status:{22:{state:'running',done:1,total:2}},command(){},review(){}}});};draw();window.ready=true;
 window.detailRequests=0;window.lazy=()=>renderPipelines(document.getElementById('history'),[{...batch(22),summary_only:true}],{},{command(){},options(){},loadDetails:async()=>{detailRequests++;await new Promise(r=>setTimeout(r,100));return {imports:[batch(22)],pipeline:{},issues:{imports:[]}};}});
 </script>`);return;}
 const path=resolve('.'+req.url);if(!path.startsWith(resolve('web')+'\\')){res.writeHead(404).end();return;}
 res.setHeader('Content-Type',path.endsWith('.css')?'text/css':'text/javascript');res.end(readFileSync(path));
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const profile=join(out,'profile'),chrome=spawn(chromePath(),['--headless','--no-first-run','--remote-debugging-port=0',`--user-data-dir=${profile}`,`http://127.0.0.1:${server.address().port}`],{windowsHide:true,stdio:'ignore'});
let ws;
try{
 for(let n=0;!existsSync(join(profile,'DevToolsActivePort'))&&n<100;n++)await sleep(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0],tabs=await(await fetch(`http://127.0.0.1:${port}/json`)).json();
 ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.onopen=r);
 let id=0;const pending=new Map();ws.onmessage=e=>{const m=JSON.parse(e.data);if(pending.has(m.id)){pending.get(m.id)(m);pending.delete(m.id);}};
 const cdp=(method,params={})=>new Promise(r=>{pending.set(++id,r);ws.send(JSON.stringify({id,method,params}));});
 const run=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert(!r.result.exceptionDetails,JSON.stringify(r));return r.result.result.value;};
 for(let n=0;n<100&&!await run('window.ready');n++)await sleep(100);
 assert.equal(await run('window.ready'),true,JSON.stringify(await run('window.errors')));
 assert.deepEqual(await run('[...document.querySelectorAll(".import-list-section>h2")].map(e=>e.textContent)'),['New Import','Previous Imports']);
 assert.equal(await run('document.querySelectorAll(".import-list-section:last-child .import-group").length'),2);
 assert.equal(await run('document.querySelector("[data-job=\\"22\\"] .import-summary-status").textContent'),'Running Discover people · 50.0%');
 await run('window.originalRow=document.querySelector("[data-job=\\"22\\"]");updatePeople({})');
 assert.equal(await run('originalRow.querySelector(".import-summary-status").textContent'),'Imported');
 await run('updatePeople({22:{state:"running"}})');
 assert.equal(await run('originalRow.querySelector(".import-summary-status").textContent'),'Running Discover people');
 assert(await run('originalRow===document.querySelector("[data-job=\\"22\\"]")'));
 await run('document.querySelector("#manualImport details").open=true;source.value="F:/Keep my input";source.focus();draw()');
 assert.equal(await run('source.value'),'F:/Keep my input');
 assert.equal(await run('document.activeElement.id'),'source');
 await run('draw(true)');
 assert.equal(await run('document.querySelector("#currentImportHeading").textContent'),'Current Import');
 assert.equal(await run('document.querySelector(".import-list-section:first-child .import-group").dataset.job'),'24');
 await run('draw(false)');
 assert.equal(await run('document.querySelector("#manualImport").hidden'),false);
 assert.equal(await run('source.value'),'F:/Keep my input');
 await run('document.querySelector("#manualImport details").open=false');
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await run('document.documentElement.scrollWidth<=innerWidth'));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`${width}.png`),Buffer.from(shot.result.data,'base64'));
 }
 await run('lazy()');assert.equal(await run('detailRequests'),0);
 await run('document.querySelector(".import-group").open=true');
 for(let n=0;n<50&&!await run('document.querySelector(".import-group").dataset.detailsLoaded');n++)await sleep(30);
 assert.equal(await run('detailRequests'),1);
 assert.equal(await run('document.querySelectorAll(".pipeline-stage").length'),4);
 assert(await run('!!document.querySelector("#manualImport")'));
 console.log('Import section browser checks passed:',out);await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
