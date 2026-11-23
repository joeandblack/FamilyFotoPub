import {chromePath} from './chrome.mjs';
import {createServer} from 'node:http';
import {readFileSync,mkdtempSync,existsSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';

const out=mkdtempSync(resolve('temp/backup-retained-')),profile=join(out,'profile');
const folders=['D:\\Backups\\'+('long-folder-name-'.repeat(12)),'<img src=x onerror=alert(1)>'];
let authorized=true,fastAvailable=true;
const run={authorized:true,state:'failed',copy_strategy:'fast-full',directory:'D:\\Backups\\current',retained_attempts:folders,started_at:'2026-11-19T00:00:00Z'};
const historyRun={...run,state:'finished',finished_at:'2026-11-19T01:50:01Z',verified_at:'2026-11-19T01:49:30Z'};
const allowed=new Set(['backup-state.mjs','backup-progress.html','backup-progress.mjs','backup-settings.mjs','confirm-dialog.mjs','deletion-progress.mjs','size.mjs','styles.css','daisyui-5.7.50.css','familyfoto.ico']);
const server=createServer((req,res)=>{
 if(req.url==='/api/backup-run'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(authorized?run:{authorized:false,state:'failed'}));return;}
 if(req.url==='/settings'){
  res.setHeader('Content-Type','text/html');
  res.end(`<html data-theme="familyfoto"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/web/styles.css"><body data-role="admin"><main><section id="storage"></section></main><script type="module">import {setupBackupSettings} from '/web/backup-settings.mjs';const api=async url=>url==='/api/backup'?{fastbackup_available:${fastAvailable},preferences:{destination:''},media_bytes:1,database_bytes:1,runs:[${JSON.stringify(historyRun)}]}:{runs:[],backups:[],preferences:{}};await setupBackupSettings({api}).load();window.ready=true;</script></body></html>`);return;
 }
 const name=req.url==='/progress'?'backup-progress.html':req.url.replace(/^\/web\//,'');
 if(!allowed.has(name)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',name.endsWith('.mjs')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.html')?'text/html':'image/x-icon');
 res.end(readFileSync(resolve('web',name)));
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const origin=`http://127.0.0.1:${server.address().port}`;
if(process.argv.includes('--serve-only')){
 console.log(`Disposable Settings fixture: ${origin}/settings`);
 await new Promise(()=>{});
}
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,origin+'/progress'],{windowsHide:true,stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));let ws;
try{
 for(let n=0;n<100&&!existsSync(join(profile,'DevToolsActivePort'));n++)await sleep(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 const pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let next=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++next;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description);return r.result.value;};
 for(let n=0;n<100&&!await evaluate('document.querySelectorAll("#runRetainedFolders li").length===2');n++)await sleep(100);
 assert.deepEqual(await evaluate('[...document.querySelectorAll("#runRetainedFolders li")].map(e=>e.textContent)'),folders);
 assert.equal(await evaluate('document.querySelectorAll("#runRetainedFolders img").length'),0);
 for(const width of [390,1200]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`progress-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 authorized=false;await sleep(1800);
 assert.equal(await evaluate('document.getElementById("runRetained").hidden'),true);
 assert.equal(await evaluate('document.querySelectorAll("#runRetainedFolders li").length'),0);
 await cdp('Page.navigate',{url:origin+'/settings'});
 for(let n=0;n<100&&!await evaluate('window.ready===true');n++)await sleep(100);
 assert.deepEqual(await evaluate('[...document.querySelectorAll("#backupRuns td ul li")].map(e=>e.textContent)'),folders);
 assert.equal(await evaluate('document.querySelectorAll("#backupRuns img").length'),0);
 assert.equal(await evaluate('document.querySelectorAll("#backupRuns th")[2].textContent'),'Total time');
 assert.equal(await evaluate('document.querySelector("#backupRuns tbody tr").cells[2].textContent'),'1h 50m 1s');
 assert.equal(await evaluate('document.querySelector("#backupRuns tbody tr:nth-child(2) td").colSpan'),6);
 assert.equal(await evaluate('getComputedStyle(document.querySelector("#backupRuns td[colspan]")).whiteSpace'),'normal');
 assert.equal(await evaluate('document.querySelectorAll("#restoreRuns img").length'),0);
 assert.equal(await evaluate('document.getElementById("backupStrategyField").hidden'),false);
 assert.equal(await evaluate('document.querySelector("input[name=backupStrategy]:checked").value'),'fast-full');
 assert.deepEqual(await evaluate('[...document.querySelectorAll("input[name=backupStrategy]")].map(o=>o.value)'),['fast-full','standard']);
 assert.equal(await evaluate('document.querySelector("select#backupStrategy")'),null);
 assert.match(await evaluate('document.getElementById("backupStrategyDescription").textContent'),/full verification of every file/);
 assert.equal(await evaluate('document.getElementById("backupFastHelp").textContent'),await evaluate('document.getElementById("backupStrategyDescription").textContent'));
 await evaluate('document.getElementById("backupUnprotected").checked=true;document.getElementById("backupStrategyStandard").click()');
 assert.match(await evaluate('document.getElementById("backupStrategyDescription").textContent'),/Resumable/);
 assert.equal(await evaluate('document.getElementById("backupStrategyFast").checked'),false);
 assert.equal(await evaluate('document.getElementById("backupUnprotected").checked'),false);
 await evaluate('document.getElementById("backupStrategyFast").click();document.getElementById("backupStrategyFast").focus()');
 assert.equal(await evaluate('getComputedStyle(document.getElementById("backupFastHelp")).visibility'),'hidden','keyboard focus must not obscure the other option');
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowRight',code:'ArrowRight',windowsVirtualKeyCode:39});
 await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowRight',code:'ArrowRight',windowsVirtualKeyCode:39});
 assert.equal(await evaluate('document.getElementById("backupStrategyStandard").checked'),true);
 assert.match(await evaluate('document.getElementById("backupStrategyDescription").textContent'),/Resumable/);
 await evaluate('document.getElementById("backupStrategyFast").click()');
 await evaluate('document.getElementById("backupStrategyField").disabled=true');
 assert.equal(await evaluate('document.getElementById("backupStrategyFast").matches(":disabled")&&document.getElementById("backupStrategyStandard").matches(":disabled")'),true);
 await evaluate('document.getElementById("backupStrategyField").disabled=false');
 assert.equal(await evaluate('document.getElementById("startBackup").disabled'),false);
 for(const width of [390,1200]){
  const touch=width===390;
  await cdp('Emulation.setTouchEmulationEnabled',{enabled:touch});
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  assert.equal(await evaluate('matchMedia("(hover:hover) and (pointer:fine)").matches'),!touch);
  assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
  await evaluate('document.getElementById("backupStrategyField").scrollIntoView();document.activeElement.blur()');
  const target=await evaluate('(()=>{const r=document.getElementById("backupStrategyFast").parentElement.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()');
  await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',...target});
  assert.equal(await evaluate('getComputedStyle(document.getElementById("backupFastHelp")).visibility'),touch?'hidden':'visible');
  if(touch){
   await cdp('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{...target}]});
   await cdp('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   await evaluate('document.getElementById("backupStrategyFast").focus()');
   assert.equal(await evaluate('getComputedStyle(document.getElementById("backupFastHelp")).visibility'),'hidden','touch and retained focus must not expose a sticky tooltip');
   assert.match(await evaluate('document.getElementById("backupStrategyDescription").textContent'),/full verification/);
   const standard=await evaluate('(()=>{const r=document.getElementById("backupStrategyStandard").getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()');
   await cdp('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{...standard}]});
   await cdp('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   assert.equal(await evaluate('document.getElementById("backupStrategyStandard").checked'),true);
   assert.match(await evaluate('document.getElementById("backupStrategyDescription").textContent'),/Resumable/);
   await evaluate('document.getElementById("backupStrategyFast").click()');
  }
  assert.equal(await evaluate('(()=>{const r=document.getElementById("backupFastHelp").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;})()'),true);
  const methodShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`method-${width}.png`),Buffer.from(methodShot.data,'base64'));
  await evaluate('document.getElementById("backupRuns").scrollIntoView()');
  const historyShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`history-${width}.png`),Buffer.from(historyShot.data,'base64'));
 }
 const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'history.png'),Buffer.from(shot.data,'base64'));
 fastAvailable=false;
 await evaluate('window.ready=false');
 await cdp('Page.navigate',{url:origin+'/settings'});
 for(let n=0;n<100&&!await evaluate('window.ready===true');n++)await sleep(100);
 assert.equal(await evaluate('document.getElementById("backupStrategyField").hidden'),true);
 assert.equal(await evaluate('document.querySelector("input[name=backupStrategy]:checked").value'),'standard');
 assert.equal(await evaluate('document.getElementById("backupStrategyFast").disabled'),true);
 console.log('Retained folder browser checks passed; screenshots: '+out);
}finally{
 if(ws)ws.close();
 if(chrome.exitCode===null){chrome.kill();await new Promise(r=>chrome.once('exit',r));}
 await new Promise(r=>server.close(r));
}
