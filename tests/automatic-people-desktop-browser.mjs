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
 await wait("document.querySelector('#job-selection')?.options.length>=1&&!document.querySelector('#job-start').disabled");await pause(1200);
 assert.equal(await evaluate("document.querySelector('#job-selection').value"),'year:2025');
 assert.equal(await evaluate("fetch('/api/automatic-people/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:'unsafe',selection:'year:2025',path:'C:/private'})}).then(r=>r.status)"),422);
 await evaluate("document.querySelector('#job-start').click()");
 await wait("document.querySelector('#job-controls').dataset.state==='running'");
 await evaluate("document.querySelector('#job-pause').click()");await wait("document.querySelector('#job-controls').dataset.state==='paused'");
 await pause(2500);
 for(let n=0;n<45;n++){
  const status=await evaluate("document.querySelector('#job-controls').dataset.state");if(status==='completed')break;
  if(['paused','waiting-service','waiting-worker','awaiting-receipt'].includes(status))await evaluate("document.querySelector('#job-resume').click()");
  await pause(2000);
 }
 await wait("document.querySelector('#job-controls').dataset.state==='completed'&&document.querySelectorAll('#groups img').length===4");
 await wait("[...document.querySelectorAll('img.photo')].every(i=>i.complete&&i.naturalWidth>0)");
 const initial=await evaluate("fetch('/api/automatic-people').then(r=>r.json())");const ids=initial.groups.map(g=>g.id);
 const named=initial.groups.find(g=>!g.optional_outlier),other=initial.groups.find(g=>g.optional_outlier);
 const nameGroup=async(gid,name)=>{await evaluate(`document.querySelector('[data-group="${gid}"] input').value=${JSON.stringify(name)};document.querySelector('[data-group="${gid}"] form').requestSubmit()`);await wait(`document.querySelector('[data-group="${gid}"] h2')?.textContent===${JSON.stringify(name)}`);};
 await nameGroup(named.id,'Desktop test A');await evaluate("document.querySelector('#outliers').parentElement.open=true");await nameGroup(other.id,'Desktop test B');
 const moved=named.faces[0].detection_id,ignored=named.faces[1].detection_id;
 await evaluate(`document.querySelector('[data-face="${moved}"] details').open=true;document.querySelector('[data-face="${moved}"] .move-face').click()`);
 await wait(`!!document.querySelector('[data-group="${other.id}"] [data-face="${moved}"]')`);
 await evaluate(`document.querySelector('[data-face="${ignored}"] details').open=true;document.querySelector('[data-face="${ignored}"] .ignore-face').click()`);
 await wait(`!document.querySelector('[data-face="${ignored}"]')`);
 assert.equal(await evaluate(`fetch('/api/automatic-people/thumbnails/${ignored}').then(r=>r.status)`),404);
 await pause(1200);await evaluate("document.querySelector('#job-start').click()");
 await wait("document.querySelector('#job-controls').dataset.state==='queued'||document.querySelector('#job-controls').dataset.state==='running'");
 await evaluate("document.querySelector('#job-cancel').click()");await wait("document.querySelector('#job-controls').dataset.state==='cancelled'");
 await pause(2000);await evaluate("document.querySelector('#job-start').click()");
 await wait("document.querySelector('#job-controls').dataset.state==='completed'");
 const final=await evaluate("fetch('/api/automatic-people').then(r=>r.json())");
 assert.deepEqual(final.groups.map(g=>g.id).sort(),ids.sort());
 assert(final.groups.find(g=>g.id===other.id).faces.some(f=>f.detection_id===moved&&f.origin==='manual'));
 assert(!final.groups.some(g=>g.faces.some(f=>f.detection_id===ignored)));
 assert.equal(await evaluate("document.querySelectorAll('input[type=checkbox]').length"),0);
 assert(await evaluate("document.documentElement.scrollWidth<=innerWidth"));
 const shot=await cdp('Page.captureScreenshot');writeFileSync(join(root,'desktop.png'),Buffer.from(shot.data,'base64'));
 const result={ok:true,browser:'Installed Windows Chrome',authentication:'existing login/session',checks:['trusted year selection','real thumbnails','background start','pause/resume','cancel','name once','manual move','ignore','stable groups and correction after replay','unauthenticated denial','no face confirmation'],group_ids:ids,moved,ignored};
 writeFileSync(join(root,'desktop-browser.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));await cdp('Browser.close');
}finally{ws?.close();chrome.kill();}
