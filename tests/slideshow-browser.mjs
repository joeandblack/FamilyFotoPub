import {chromePath} from './chrome.mjs';
// Synthetic media only; no production API, files or credentials are used.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(resolve('temp/slideshow-'));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const fixture=`<script type="module">
import {setupViewer} from '/web/viewer.mjs';
const items=[1,2,3].map(id=>({id,kind:'image',name:id+'.jpg',relative_path:id+'.jpg',size:10}));
window.openTest=setupViewer({api:async()=>({items:[items[2]],page:2,pages:2}),context:()=>({items:items.slice(0,2),page:1,pages:2,filters:{kind:'image'}}),fields:()=>{},size:()=>'',changed:()=>{}});
window.openTest(1);
</script>`;
const server=createServer((req,res)=>{
  if(req.url==='/'){
    res.setHeader('Content-Type','text/html');
    res.end(readFileSync('web/index.html','utf8').replace(/<script type="module"[^>]*>[\s\S]*?<\/script>/g,'')+fixture);
  }else if(req.url.startsWith('/web/')&&!req.url.includes('..')){
    res.setHeader('Content-Type',req.url.endsWith('.css')?'text/css':'text/javascript');res.end(readFileSync('.'+req.url));
  }else if(req.url.startsWith('/api/media/')){
    res.setHeader('Content-Type','image/png');setTimeout(()=>res.end(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64')),req.url.includes('/2/')?4500:0);
  }else {res.statusCode=404;res.end();}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const url=`http://127.0.0.1:${server.address().port}/`,profile=join(out,'profile');
const chrome=spawn(chromePath(),['--headless','--no-first-run','--remote-debugging-port=0',`--user-data-dir=${profile}`,url],{windowsHide:true,stdio:'ignore'});
let ws;
try{
  for(let n=0;n<150&&!existsSync(join(profile,'DevToolsActivePort'));n++)await pause(100);
  const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
  let tabs;for(let n=0;n<100;n++){tabs=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();if(tabs.some(t=>t.url===url))break;await pause(100);}
  ws=new WebSocket(tabs.find(t=>t.url===url).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
  let id=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
  const cdp=(method,params={})=>new Promise((resolve,reject)=>{pending.set(++id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
  const run=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description);return r.result.value;};
  const wait=async expr=>{for(let n=0;n<150;n++){if(await run(expr))return;await pause(100);}throw Error('Timed out: '+expr);};
  await wait('document.querySelector("#fullMedia img")?.naturalWidth>0');
  assert(await run("getComputedStyle(document.getElementById('captureTime')).display==='none'"));
  await run("document.getElementById('captureTime').dataset.error='true';document.getElementById('captureTime').textContent='Test error'");
  assert(await run("getComputedStyle(document.getElementById('captureTime')).display!=='none'"));
  await run("delete document.getElementById('captureTime').dataset.error;document.getElementById('captureTime').textContent=''");
  const pointerClick=async selector=>{
    const p=await run(`(()=>{const r=document.querySelector('${selector}').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...p,button:'left',clickCount:1});
    await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',...p,button:'left',clickCount:1});
  };
  await pointerClick('#photoFullscreen');
  await wait("document.fullscreenElement===document.querySelector('.viewer-surface')");
  await wait("document.getElementById('photoFullscreen').title==='Exit fullscreen'");
  await pointerClick('#photoFullscreen');await wait('!document.fullscreenElement');
  await pointerClick('#photoFullscreen');await wait('!!document.fullscreenElement');
  await run("document.getElementById('closeViewer').click()");await wait('!document.fullscreenElement');
  await run('openTest(1)');await wait('document.querySelector("#fullMedia img")?.naturalWidth>0');
  assert(await run("getComputedStyle(document.querySelector('.viewer-interval')).pointerEvents==='auto'"));
  await run("document.querySelector('.viewer-interval').value='3';document.querySelector('.viewer-interval').dispatchEvent(new Event('change'));document.getElementById('viewerAutoplay').click()");
  await pause(3300);
  assert(await run("document.querySelector('#fullMedia img').src.includes('/1/')&&!Array.from(document.querySelectorAll('#fullMedia .viewer-message')).some(e=>!e.hidden)"));
  await run("document.getElementById('viewerAutoplay').click()");
  await pause(1400);
  assert(await run("document.querySelector('#fullMedia img').src.includes('/1/')&&document.getElementById('viewerAutoplay').getAttribute('aria-pressed')==='false'"));
  await run("window.loadingFlashes=0;new MutationObserver(()=>{if(Array.from(document.querySelectorAll('#fullMedia .viewer-message')).some(e=>!e.hidden))loadingFlashes++;}).observe(document.getElementById('fullMedia'),{childList:true,subtree:true});document.getElementById('viewerAutoplay').click()");
  await wait("document.querySelector('#fullMedia img').src.includes('/2/')");
  await wait("document.querySelector('#fullMedia img').src.includes('/3/')");
  await wait("document.getElementById('viewerAutoplay').getAttribute('aria-pressed')==='false'");
  assert.equal(await run('window.loadingFlashes'),0);
  for(const width of [1280,390]){
    await cdp('Emulation.setDeviceMetricsOverride',{width,height:800,deviceScaleFactor:1,mobile:width<500});
    await pause(100);
    assert(await run("document.getElementById('viewer').scrollHeight<=document.getElementById('viewer').clientHeight"));
    assert(await run("(()=>{const a=document.querySelector('.viewer-navigation:first-child').getBoundingClientRect(),b=document.querySelector('.viewer-navigation:last-child').getBoundingClientRect();return (a.right<=b.left||a.bottom<=b.top)&&b.right<=innerWidth;})()"));
    assert(await run("(()=>{const p=document.getElementById('viewerAutoplay').getBoundingClientRect(),s=document.querySelector('.viewer-interval').getBoundingClientRect(),z=document.querySelector('.photo-zoom-controls').getBoundingClientRect();return s.top>=p.bottom&&z.left>=p.right&&z.top<100&&z.right<=innerWidth;})()"));
    const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`viewer-${width}.png`),Buffer.from(shot.data,'base64'));
  }
  await run("document.getElementById('closeViewer').click();openTest(1)");
  await cdp('Network.enable');await cdp('Network.setCacheDisabled',{cacheDisabled:true});
  await run("document.getElementById('viewerNext').click()");
  assert(await run("document.querySelector('#fullMedia img').src.includes('/2/')&&!document.getElementById('viewerPrevious').disabled"));
  await run("document.getElementById('viewerPrevious').click()");
  assert(await run("document.querySelector('#fullMedia img').src.includes('/1/')"));
  await run("document.getElementById('viewerAutoplay').click();document.getElementById('closeViewer').click()");
  await pause(3200);assert(await run("!document.getElementById('viewer').open&&document.getElementById('viewerAutoplay').getAttribute('aria-pressed')==='false'"));
  console.log(JSON.stringify({ok:true,checks:['photo timer','cross-page advance','stop at end','close cancels','desktop/mobile fit'],out}));
  await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
