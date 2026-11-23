import {chromePath} from './chrome.mjs';
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(resolve('temp/album-cover-')),sleep=ms=>new Promise(r=>setTimeout(r,ms));
const server=createServer((req,res)=>{
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(`<meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/web/styles.css"><script type="module">import {editCover} from '/web/album-cover.mjs';window.saved=null;window.launch=()=>editCover({album:{id:1,cover_id:1},api:async(u,o)=>{window.saved=JSON.parse(o.body);return {};},onSaved:()=>{}});launch();</script>`);return;}
 if(req.url.includes('/thumbnail')){res.setHeader('Content-Type','image/svg+xml');res.end('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900"><rect width="600" height="900" fill="#b4d9cb"/><rect x="60" y="80" width="480" height="740" fill="#34624e"/><circle cx="300" cy="450" r="160" fill="#efcb76"/></svg>');return;}
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
 for(let n=0;n<100&&!await run('!!document.querySelector(".album-cover-preview img")?.naturalWidth');n++)await sleep(100);
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await run('(()=>{const r=document.querySelector("dialog").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})()'));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`${width}.png`),Buffer.from(shot.result.data,'base64'));
 }
 await run('document.querySelector("input").value=2;document.querySelector("input").dispatchEvent(new Event("input"))');
 const point=await run('(()=>{const r=document.querySelector(".album-cover-preview").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()');
 await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:point.x+30,y:point.y+30,buttons:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x+30,y:point.y+30,button:'left',clickCount:1});
 await run('[...document.querySelectorAll("button")].find(b=>b.textContent==="Save").click()');await sleep(100);
 const saved=await run('saved');assert.equal(saved.zoom,2);assert(saved.x>0&&saved.y>0);assert.equal(saved.media_id,1);
 await run('launch()');await sleep(100);await run('[...document.querySelectorAll("button")].find(b=>b.textContent==="Fit").click();[...document.querySelectorAll("button")].find(b=>b.textContent==="Cancel").click()');assert.deepEqual(await run('saved'),saved);
 console.log('Cover editor browser checks passed:',out);await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
