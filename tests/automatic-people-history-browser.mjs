import {chromePath} from './chrome.mjs';
// Authenticated real-photo isolated app. Existing login values arrive only on stdin.
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const {base,root,username,password}=JSON.parse(readFileSync(0,'utf8'));
assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
assert.equal((await fetch(base+'/api/automatic-people')).status,401);
assert.equal((await fetch(base+'/api/automatic-people/thumbnails/1')).status,401);
const profile=mkdtempSync(join(resolve('temp'),'desktop-people-chrome-')),pause=ms=>new Promise(r=>setTimeout(r,ms));
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1440,1200',base+'/login'],{windowsHide:true,stdio:'ignore'});let ws;
try{
 for(let n=0;n<150&&!existsSync(join(profile,'DevToolsActivePort'));n++)await pause(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];let pages;
 for(let n=0;n<100;n++){pages=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();if(pages.some(p=>p.url.startsWith(base)))break;await pause(100);}
 ws=new WebSocket(pages.find(p=>p.url.startsWith(base)).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const wait=async expression=>{for(let n=0;n<600;n++){if(await evaluate(expression))return;await pause(100);}throw Error('Timed out: '+expression);};
 await wait("!!document.querySelector('#loginForm')");
 await evaluate(`document.querySelector('[name=username]').value=${JSON.stringify(username)};document.querySelector('[name=password]').value=${JSON.stringify(password)};document.querySelector('#loginForm').requestSubmit()`);
 await wait("!location.pathname.includes('login')");
 await cdp('Page.navigate',{url:base+'/?jobs=1&desktop=1'});
 await wait("document.querySelector('#job-selection')?.options.length>=2&&!document.querySelector('#job-start').disabled");await pause(1200);

 const choices=await evaluate("fetch('/api/automatic-people/selections').then(r=>r.json())");
 assert(choices.some(c=>c.id==='import:6'));
 await evaluate("document.querySelector('#job-selection').value='import:6'");
 const initial=await evaluate("fetch('/api/automatic-people').then(r=>r.json())");
 const origin=initial.groups.find(g=>g.name==='Desktop test A'),target=initial.groups.find(g=>g.name==='Desktop test B');
 const did=origin.faces[0].detection_id;
 await evaluate(`document.querySelector('[data-group="${origin.id}"] .group-detail').click()`);
 await wait("document.querySelector('#group-detail')?.open");
 assert.equal(await evaluate("document.querySelectorAll('#group-detail img').length"),origin.faces.length);
 await evaluate(`document.querySelector('#group-detail [data-face="${did}"] details').open=true;document.querySelector('#group-detail [data-face="${did}"] .move-face').click()`);
 await wait("!!document.querySelector('#group-detail .undo-correction')");
 const event=await evaluate("document.querySelector('#group-detail .undo-correction').parentElement.dataset.event");
 await cdp('Page.reload');await wait("document.querySelector('#job-selection')?.options.length>=2");
 await wait(`!!document.querySelector('[data-group="${origin.id}"] .group-detail')`);
 await evaluate(`document.querySelector('[data-group="${origin.id}"] .group-detail').click()`);
 await wait(`!!document.querySelector('#group-detail [data-event="${event}"] .undo-correction')`);
 await evaluate("document.querySelector('#group-detail .undo-correction').click()");
 await wait(`!!document.querySelector('#group-detail [data-face="${did}"]')&&!document.querySelector('#group-detail .undo-correction')`);
 const final=await evaluate("fetch('/api/automatic-people').then(r=>r.json())");
 const memberships=state=>state.groups.map(g=>[g.id,g.faces.map(f=>[f.detection_id,f.origin])]);
 assert.deepEqual(memberships(final),memberships(initial));
 const shot=await cdp('Page.captureScreenshot');writeFileSync(join(root,'history.png'),Buffer.from(shot.data,'base64'));
 const result={ok:true,browser:'Installed Windows Chrome',checks:['trusted Import 6 selection','full visible group detail','manual correction','history survives reload','targeted undo restores exact membership origins'],event_id:Number(event),face_id:did,gpu_inference:false};
 writeFileSync(join(root,'history-browser.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 await cdp('Network.clearBrowserCookies');await cdp('Browser.close');
}finally{ws?.close();chrome.kill();}
