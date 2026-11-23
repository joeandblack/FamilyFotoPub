import {chromePath} from './chrome.mjs';
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(resolve('temp/album-edit-')),sleep=ms=>new Promise(r=>setTimeout(r,ms));
const server=createServer((req,res)=>{
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(`<meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/web/styles.css"><main><header><h1 id="heading"></h1><div id="summary"></div><input id="search"></header><section id="library"></section></main><script type="module">
 import {setupAlbums} from '/web/albums.mjs';
 const album={id:1,name:'Fixture album',description:'Family photographs',cover_id:1,item_count:3};
 window.calls=[];window.albumClosed=false;
 const albums=setupAlbums({api:async(u,o)=>{calls.push([u,o?.method]);return album;},openAlbum:async id=>{albumClosed=id===null;},refresh:async()=>{},notice:message=>{throw Error(message);}});
 await albums.detail(1);window.ready=true;
 </script>`);return;}
 if(req.url.includes('/thumbnail')){res.setHeader('Content-Type','image/svg+xml');res.end('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400" height="300" fill="#34624e"/></svg>');return;}
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
 assert.deepEqual(await run('[...document.querySelectorAll("#albumHeader button")].map(b=>b.textContent)'),['Edit album','All albums']);
 await run('[...document.querySelectorAll("#albumHeader button")].find(b=>b.textContent==="Edit album").click()');
 assert(await run('[...document.querySelectorAll("#albumDialog button")].some(b=>b.textContent==="Adjust cover")'));
 assert(await run('[...document.querySelectorAll("#albumDialog button")].some(b=>b.textContent==="Delete album")'));
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await run('(()=>{const r=document.querySelector("#albumDialog").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth})()'));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`${width}.png`),Buffer.from(shot.result.data,'base64'));
 }
 await run('[...document.querySelectorAll("#albumDialog button")].find(b=>b.textContent==="Adjust cover").click()');
 assert(await run('document.querySelector(".album-cover-editor").open'));
 await run('[...document.querySelectorAll(".album-cover-editor button")].find(b=>b.textContent==="Cancel").click()');
 assert(await run('document.querySelector("#albumDialog").open'));
 await run('[...document.querySelectorAll("#albumDialog button")].find(b=>b.textContent==="Delete album").click()');
 assert.equal(await run('calls.some(([u,m])=>m==="DELETE")'),false);
 console.log('Album edit desktop/mobile checks passed:',out);await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
