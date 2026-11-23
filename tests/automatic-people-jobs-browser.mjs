import {chromePath} from './chrome.mjs';
// Installed Windows Chrome, separate synthetic application URL supplied by pytest.
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const url=process.argv[2]+'/?jobs=1',scenario=process.argv[3]||'complete';
assert.match(url,/^http:\/\/127\.0\.0\.1:\d+\/\?jobs=1$/);
const out=mkdtempSync(join(resolve('temp'),'auto-jobs-browser-')),profile=join(out,'profile'),pause=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1280,900',url],{windowsHide:true,stdio:'ignore'});let ws;
try{
 for(let n=0;n<150&&!existsSync(join(profile,'DevToolsActivePort'));n++)await pause(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];let pages;
 for(let n=0;n<100;n++){pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();if(pages.some(p=>p.url===url))break;await pause(100);}
 ws=new WebSocket(pages.find(p=>p.url===url).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const wait=async expression=>{for(let n=0;n<300;n++){if(await evaluate(expression))return;await pause(100);}throw Error('Timeout: '+expression);};
 await wait("!!document.querySelector('#job-start')&&!document.querySelector('#job-start').disabled");await evaluate("document.querySelector('#job-start').click()");
 await wait("document.querySelector('#job-controls').dataset.state==='running'");
 if(scenario==='cancel'){
  await evaluate("document.querySelector('#job-cancel').click()");await wait("document.querySelector('#job-controls').dataset.state==='cancelled'");
  assert(await evaluate("document.querySelector('#job-resume').disabled"));
 }else{
  await evaluate("document.querySelector('#job-pause').click()");await wait("document.querySelector('#job-controls').dataset.state==='paused'");
  await pause(2500);await evaluate("document.querySelector('#job-resume').click()");
  await wait("document.querySelector('#job-controls').dataset.state==='completed'");
  await evaluate("location.hash='#review-faces'");
  await wait("document.querySelectorAll('#groups .card,#review-groups .card,#outliers .card').length>0");
  assert(await evaluate("!!document.querySelector('#review-groups .unnamed-person-card,#outliers .unnamed-person-card')"));
  assert.equal(await evaluate("document.querySelectorAll('input[type=checkbox]').length"),0);
 }
 const job=await evaluate("document.querySelector('#job-controls').dataset.job");
 const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,scenario+'.png'),Buffer.from(shot.data,'base64'));
 console.log(JSON.stringify({ok:true,browser:'Installed Windows Chrome',scenario,job,artifacts:out}));await cdp('Browser.close');
}finally{ws?.close();chrome.kill();}
