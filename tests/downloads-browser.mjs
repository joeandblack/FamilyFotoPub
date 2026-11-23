import {chromePath} from './chrome.mjs';
// Installed Windows Chrome; the Python fixture proxies an isolated TestClient/schema.
import {readFileSync,writeFileSync,mkdtempSync,existsSync,mkdirSync,readdirSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const fixture=JSON.parse(readFileSync(process.argv[2],'utf8'));
const {url,ids,job}=fixture;
assert.equal(new URL(url).hostname,'127.0.0.1');assert.notEqual(new URL(url).port,'4340');
const root=resolve('.'),out=mkdtempSync(join(root,'temp','downloads-browser-')),profile=join(out,'profile');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--disable-component-update','--disable-extensions','--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1280,900',url],{windowsHide:true,stdio:'ignore'});
let ws;const checks=[];
try{
 for(let n=0;n<150&&!existsSync(join(profile,'DevToolsActivePort'));n++)await pause(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 let pages;for(let n=0;n<100;n++){pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();if(pages.some(p=>p.url===url))break;await pause(100);}
 ws=new WebSocket(pages.find(p=>p.url===url).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let counter=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++counter;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text+': '+r.exceptionDetails.exception?.description);return r.result.value;};
 const wait=async expression=>{for(let n=0;n<100;n++){if(await evaluate(expression))return;await pause(50);}throw Error('Timed out: '+expression+'; '+await evaluate('document.body.innerText'));};
 const run=expression=>evaluate(expression);
 const count=()=>evaluate("document.querySelectorAll('.tile.selected').length");
 const click=action=>run(`document.querySelector('#trashSelection [data-action="${action}"]').click()`);
 const rect=async index=>evaluate(`(()=>{const r=document.querySelectorAll('#grid .tile')[${index}].getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()`);
 const mouse=(type,x,y,extra={})=>cdp('Input.dispatchMouseEvent',{type,x,y,button:'left',buttons:type==='mouseReleased'?0:1,clickCount:1,...extra});
 const drag=async(a,b,modifiers=0)=>{await mouse('mousePressed',a.x,a.y,{modifiers});for(let n=1;n<=8;n++)await mouse('mouseMoved',a.x+(b.x-a.x)*n/8,a.y+(b.y-a.y)*n/8,{modifiers});const box=await evaluate("(()=>{const r=document.querySelector('.selection-rectangle').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};})()");
  for(const [key,value] of Object.entries({left:Math.min(a.x,b.x),top:Math.min(a.y,b.y),right:Math.max(a.x,b.x),bottom:Math.max(a.y,b.y)}))assert(Math.abs(box[key]-value)<2,`Marquee ${key}: ${box[key]} != ${value}`);
  await mouse('mouseReleased',b.x,b.y,{modifiers});};


 await wait("document.querySelectorAll('#grid .tile').length===3&&!document.querySelector('#trashSelection [data-action=select]').disabled");
 await evaluate("Object.defineProperty(crypto,'randomUUID',{value:undefined,configurable:true})");
 const downloadPath=join(out,'downloads');mkdirSync(downloadPath);
 await cdp('Browser.setDownloadBehavior',{behavior:'allow',downloadPath,eventsEnabled:true});
 const archives=[],singles=[];
 const select=async wanted=>{if(!await evaluate("document.getElementById('grid').classList.contains('selection-mode')"))await click('select');await click('clear');for(const id of wanted)await run(`document.querySelector('#grid [data-id="${id}"]').click()`);assert.equal(await count(),wanted.length);};
 const downloaded=async before=>{for(let i=0;i<200;i++){const paths=readdirSync(downloadPath).filter(n=>/\.(zip|jpg)$/.test(n)&&!before.includes(n));if(paths.length)return join(downloadPath,paths[0]);await pause(50);}throw Error('No completed download');};
 const downloadZip=async wanted=>{
  await select(wanted);await click('download');await wait("document.getElementById('saveDownloadZip')?.hidden===false");
  // Selection changes after preparation must not change the download snapshot.
  await click('clear');
  const before=readdirSync(downloadPath);
  const r=await evaluate("(()=>{const r=document.getElementById('saveDownloadZip').getBoundingClientRect();return {x:r.x+20,y:r.y+20};})()");
  await mouse('mousePressed',r.x,r.y);await mouse('mouseReleased',r.x,r.y);
  const path=await downloaded(before);archives.push({path,ids:wanted});
  await run("document.querySelector('#downloadDialog button').click()");
 };
 await click('select');
 await downloadZip(ids);
 await downloadZip(ids.slice(0,2));
 await downloadZip(ids.slice(0,2));
 checks.push('Library downloads one actual ZIP per user action, repeated requests and immutable selection snapshots');
 await select(ids.slice(0,2));await click('download');await run("document.querySelector('#downloadDialog button').click()");await pause(100);
 assert.equal(await evaluate("document.getElementById('downloadDialog').open"),false);
 checks.push('Cancel during preparation starts no archive');
 await select([ids[0]]);let before=readdirSync(downloadPath);await click('download');singles.push({path:await downloaded(before),id:ids[0]});
 checks.push('Single selection saves a direct original');
 // Move only disposable fixtures to Trash, using the actual API.
 await evaluate(`Promise.all(${JSON.stringify(ids)}.map(id=>fetch('/api/media/'+id+'/trash/trash',{method:'POST'}))).then(rs=>rs.every(r=>r.ok))`);
 await run("document.querySelector('nav [data-view=trash]').click()");await wait("document.querySelectorAll('#grid .tile').length===3&&!document.querySelector('#trashSelection [data-action=select]').disabled");await click('select');
 await downloadZip(ids);checks.push('Recently Deleted downloads every selected photo/video in one archive without restore');
 await run("document.body.dataset.role='guest'");await downloadZip(ids.slice(1));
 checks.push('Download stays available in read-only UI');
 await select(ids.slice(1));await click('download');await wait("document.getElementById('saveDownloadZip')?.hidden===false");
 await evaluate("fetch('/fixture/unavailable').then(r=>r.json())");
 const unavailable=await evaluate("(()=>{const r=document.getElementById('saveDownloadZip').getBoundingClientRect();return {x:r.x+20,y:r.y+20};})()");
 await mouse('mousePressed',unavailable.x,unavailable.y);await mouse('mouseReleased',unavailable.x,unavailable.y);
 await wait("document.querySelector('#downloadDialog [role=status]').textContent.includes('unavailable')");
 checks.push('Late missing-file error appears in the dialog; no partial archive is presented as complete');
 const screenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'trash-download.png'),Buffer.from(screenshot.data,'base64'));
 const result={ok:true,browser:'Installed Windows Chrome, real API with isolated PostgreSQL and disposable originals',checks,archives,singles,artifacts:out};
 writeFileSync(join(out,'results.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 await cdp('Browser.close').catch(()=>{});
}finally{ws?.close();chrome.kill();}
