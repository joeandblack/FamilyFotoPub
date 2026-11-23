import {chromePath} from './chrome.mjs';
import {createServer} from 'node:http';
import {readFileSync,mkdtempSync,existsSync,writeFileSync,mkdirSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';

mkdirSync('temp',{recursive:true});
const out=mkdtempSync(resolve('temp/backup-modal-')),profile=join(out,'profile');
let startCount=0,statusFailures=2,rejectStart=false,rejectPlan=false,log='first line\n',decisions=[],faceRequests=0,startBody=null;
let offline=false,appRequests=[],holdStatus=false,releaseStatus=null;
let run={id:'fixture',authorized:true,state:'capturing',finished:false,copy_strategy:'fast-full',directory:'F:/test-backup',progress:{phase:'media-transfer',state:'running',elapsed_seconds:12,files:2,total_files:4,bytes:1024,total_bytes:2048}};
const files=new Set(['backup-state.mjs','backup-progress.html','backup-progress.mjs','backup-settings.mjs','confirm-dialog.mjs','size.mjs','styles.css','familyfoto.ico','daisyui-5.7.50.css']);
const server=createServer(async(req,res)=>{
 const json=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
 if(req.url==='/application'){res.setHeader('Content-Type','text/html');res.end(readFileSync(resolve('web/index.html')));return;}
 if(req.url.startsWith('/api/')&&!req.url.startsWith('/api/backup-run')){
  appRequests.push(req.url);
  if(offline){res.setHeader('Content-Type','text/html');res.end('<!doctype html><p>Backup offline</p>');return;}
 }
 if(req.url==='/api/me'){json({username:'fixture',role:'admin',capabilities:{}});return;}
 if(req.url==='/api/status'){
  if(holdStatus){releaseStatus=()=>json({error:'expired'},401);return;}
  json({job:null,video:null,conversion:null,monitor:{state:'healthy'}});return;
 }
 if(req.url==='/settings'){
  res.setHeader('Content-Type','text/html');
  res.end(`<html data-theme="familyfoto"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/web/styles.css"><body data-role="admin"><button id="outside" onclick="window.outsideClicked=true">Other action</button><main><section id="storage"></section></main><script type="module">import {setupBackupSettings} from '/web/backup-settings.mjs';import {setupFaceService} from '/face_detect/web/face-service.mjs';const api=async(url,options)=>{const r=await fetch(url,options);const v=await r.json();if(!r.ok)throw Error(v.error);return v;};await setupBackupSettings({api}).load();window.faceService=setupFaceService({api});await faceService.load();window.ready=true;</script></body></html>`);return;
 }
 if(req.url==='/face_detect/web/face-service.mjs'){res.setHeader('Content-Type','text/javascript');res.end(readFileSync(resolve('face_detect/web/face-service.mjs')));return;}
 if(req.url==='/api/face-service'){faceRequests++;json({state:'stopped',message:'Face service stopped',start_with_app:false});return;}
 if(req.url==='/api/backup'){json({fastbackup_available:true,preferences:{destination:'F:/test-backup'},media_bytes:2048,database_bytes:10,runs:[]});return;}
 if(req.url==='/api/backup/restores'){json({runs:[],backups:[]});return;}
 if(req.url==='/api/backup/plan'){throw Error('Settings must not check twice');}
 if(req.url==='/api/backup/start'){let body='';for await(const chunk of req)body+=chunk;startBody=JSON.parse(body);startCount++;setTimeout(()=>json(rejectPlan?{code:'backup_plan_rejected',detail:'Not enough space',problems:['Not enough space: needed 10 GiB; free 1 GiB.','Enter the correct password.']}:rejectStart?{error:'fixture start rejected'}:{run:'fixture'},rejectPlan||rejectStart?409:200),1600);return;}
 if(req.url==='/api/backup-run'){if(statusFailures-->0){json({error:'handover'},503);return;}json(run);return;}
 if(req.url.startsWith('/api/backup-run/log')){const after=Number(new URL(req.url,'http://local').searchParams.get('after'));json({text:log.slice(after),next:log.length});return;}
 if(req.url==='/api/backup-run/decision'){let body='';for await(const chunk of req)body+=chunk;decisions.push(JSON.parse(body).action);json({ok:true});return;}
 if(req.url.startsWith('/api/')){json({items:[],stats:{total:0,bytes:0},years:[],page:1,pages:1,total:0,accounts:[],patterns:[],source:'fixture',destination:'fixture',revision:'fixture'});return;}
 if(/^\/(?:web|face_detect\/web)\/[a-zA-Z0-9._-]+\.(?:m?js|css|ico)$/.test(req.url)&&existsSync(resolve(req.url.slice(1)))){
  res.setHeader('Content-Type',/\.m?js$/.test(req.url)?'text/javascript':req.url.endsWith('.css')?'text/css':'image/x-icon');res.end(readFileSync(resolve(req.url.slice(1))));return;
 }
 const name=req.url==='/backup-progress'?'backup-progress.html':req.url.replace(/^\/web\//,'');
 if(!files.has(name)){res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type',name.endsWith('.mjs')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.html')?'text/html':'image/x-icon');
 res.end(readFileSync(resolve('web',name)));
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const origin=`http://127.0.0.1:${server.address().port}`;
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,origin+'/settings'],{windowsHide:true,stdio:'ignore'});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));let ws;
try{
 for(let n=0;n<100&&!existsSync(join(profile,'DevToolsActivePort'));n++)await sleep(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 const pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let next=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++next;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description);return r.result.value;};
 const wait=async expression=>{for(let n=0;n<150;n++){if(await evaluate(expression))return;await sleep(100);}throw Error('Timed out: '+expression);};
 await wait('window.ready===true');
 assert.equal(await evaluate('document.getElementById("checkBackup")'),null);
 await evaluate(`document.getElementById('backupPassword').value='fixture-only';document.getElementById('startBackup').click()`);
 await wait('!!document.querySelector("dialog[open]")');
 assert.equal(startCount,0);
 await evaluate('document.querySelector("dialog .confirm-dialog-actions button").click()');
 assert.equal(startCount,0);
 const start=async()=>{
  await evaluate(`document.getElementById('backupPassword').value='fixture-only';document.getElementById('startBackup').click()`);
  await wait('!!document.querySelector("dialog[open]")');
  await evaluate(`window.originalDialog=document.querySelector('dialog[open]');originalDialog.querySelector('.btn-primary').click()`);
  await wait('!!document.getElementById("runState")');
  assert.equal(await evaluate('document.getElementById("runState").textContent'), 'Checking backup plan...');
  assert.equal(await evaluate('document.getElementById("runProgress").hasAttribute("value")'),false);
  await wait('document.getElementById("runDetail").textContent.includes("1s elapsed")');
 };
 await start();
 assert.equal(startBody.copy_strategy,'fast-full');
 assert.equal(startBody.verification,'full');
 assert.equal(await evaluate('[...document.querySelectorAll("input[name=backupStrategy]")].every(r=>r.matches(":disabled"))'),true);
 const faceBefore=faceRequests;
 await evaluate('faceService.load()');
 assert.equal(await evaluate('originalDialog===document.querySelector("dialog[open]")'),true);
 assert.equal(await evaluate('location.pathname'),'/settings');
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
 assert.equal(await evaluate('originalDialog.open'),true);
 await evaluate('document.getElementById("outside").focus()');
 assert.equal(await evaluate('document.activeElement.id==="outside"'),false);
 await wait('document.getElementById("runDetail").textContent.includes("Copying photos")');
 assert.equal(startCount,1);assert.equal(await evaluate('document.getElementById("backupPassword").value'),'');
 await evaluate('document.getElementById("runLog").open=true');
 await wait('document.getElementById("runLogText").textContent.includes("first line")');
 log+=Array.from({length:100},(_,i)=>`copy line ${i}\n`).join('');
 await wait('document.getElementById("runLogText").textContent.includes("copy line 99")');
 assert.equal(faceRequests,faceBefore,'face-service polling stops while backup modal owns the page');
 await evaluate('document.getElementById("runLogText").scrollTop=0');
 log+='new tail\n';
 await wait('document.getElementById("runLogText").textContent.includes("new tail")');
 assert.equal(await evaluate('document.getElementById("runLogText").scrollTop'),0);
 for(const width of [390,1200]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
  assert.equal(await evaluate('(()=>{const r=originalDialog.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;})()'),true);
  assert.equal(await evaluate('originalDialog.scrollWidth<=originalDialog.clientWidth'),true);
  writeFileSync(join(out,`modal-${width}.png`),Buffer.from((await cdp('Page.captureScreenshot')).data,'base64'));
 }
 run={...run,state:'failed',error:'Test disk disconnected'};
 await wait('!document.getElementById("runDecision").hidden');
 assert.equal(await evaluate('document.getElementById("runRetry").textContent'),'Start new attempt');
 await evaluate('document.getElementById("runRetry").click()');
 for(let n=0;n<50&&!decisions.length;n++)await sleep(50);
 assert.deepEqual(decisions,['retry']);
 run={...run,state:'finished',finished:true,error:null,assurance:'full',verified_at:'2026-11-21T00:00:00Z'};
 await wait('!document.getElementById("runDone").hidden');
 assert.match(await evaluate('document.getElementById("runDoneText").textContent'),/safely on the backup disk/);
 assert.equal(await evaluate('originalDialog.open'),true);
 assert.match(await evaluate('document.getElementById("runState").textContent'),/finished/i);
 // A failed start stays in the modal, clears the password and never auto-submits again.
 rejectStart=true;await cdp('Page.navigate',{url:origin+'/settings'});await wait('window.ready===true');
 await start();await wait('document.getElementById("runState").textContent.includes("could not be confirmed")');
 assert.equal(startCount,2);
 assert.equal(await evaluate('document.getElementById("backupPassword").value'),'');
 assert.equal(await evaluate('document.getElementById("runReturn").getAttribute("href")'),'/backup-progress');
 assert.equal(await evaluate('originalDialog.open'),true);
 // Exercise real app.js timers, including an in-flight session-expiry response at handover.
 rejectStart=false;statusFailures=0;run={...run,state:'capturing',finished:false};
 await cdp('Page.navigate',{url:origin+'/application'});
 await wait('document.body.dataset.role==="admin"&&!!document.getElementById("backupSettings")');
 await evaluate('document.querySelector("[data-view=settings]").click()');
 await wait('!document.getElementById("startBackup").disabled');
 rejectPlan=true;
 await start();
 await wait('document.getElementById("runState").textContent==="The backup did not start."');
 assert.equal(await evaluate('document.getElementById("runAbout").textContent'),'FamilyFoto is still running.');
 assert.equal(await evaluate('document.querySelectorAll("#runError li").length'),2);
 assert.equal(await evaluate('document.getElementById("backupPassword").value'),'fixture-only');
 const frozen=await evaluate('document.getElementById("runDetail").textContent');await sleep(1200);
 assert.equal(await evaluate('document.getElementById("runDetail").textContent'),frozen);
 await evaluate('document.querySelector("#runDone button").click()');
 assert.equal(await evaluate('!!document.querySelector("dialog[open]")'),false);
 assert.equal(await evaluate('document.getElementById("startBackup").disabled'),false);
 assert.equal(await evaluate('document.getElementById("backupPassword").value'),'fixture-only');
 rejectPlan=false;
 holdStatus=true;
 for(let n=0;n<100&&!releaseStatus;n++)await sleep(100);
 assert.ok(releaseStatus,'a normal app status request is in flight');
 await start();
 await wait('document.getElementById("backupPassword").value===""');
 releaseStatus();offline=true;
 const requestsBefore=appRequests.length;
 await sleep(5500);
 assert.equal(appRequests.length,requestsBefore,'no normal app requests while backup modal is active');
 assert.equal(await evaluate('location.pathname'),'/application','late 401 must not navigate away from the backup');
 assert.equal(await evaluate('document.getElementById("notice").textContent.includes("Unexpected token")'),false);
 assert.equal(await evaluate('document.getElementById("sideState").textContent==="Connection lost"'),false);
 assert.equal(await evaluate('originalDialog.open'),true);
 console.log('Backup modal, logs, handover, failure controls and desktop/mobile checks passed: '+out);
}finally{
 ws?.close();if(chrome.exitCode===null){chrome.kill();await new Promise(r=>chrome.once('exit',r));}
 await new Promise(r=>server.close(r));
}
