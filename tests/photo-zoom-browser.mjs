import {chromePath} from './chrome.mjs';
// Installed Windows Chrome + real CDP mouse/touch events. All API data/actions
// are disposable fixtures; no production backend or photo data is accessed.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync,mkdirSync} from 'node:fs';
import {resolve,join,extname} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=resolve('.'),webRoot=resolve(process.env.FAMILYFOTO_WEB_ROOT||'web'),out=mkdtempSync(join(root,'temp','photo-zoom-'));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const fixture=`
window.commands=[];window.downloads=[];window.confirm=()=>true;window.failId=null;
const records=Array.from({length:160},(_,i)=>({id:i+1,kind:i===1?'video':'image',captured_at:'2026-10-14T12:34:56',name:'same.jpg',size:10,width:10,height:10,rotation:i===3?90:0,crop:i===3?{x:0.1,y:0.1,width:0.8,height:0.8}:null,relative_path:'fixture.jpg',sha256:String(i).padStart(64,'0'),year:i%2?'2020':'2021',job:i%3?1:2}));
const trashed=new Set();
window.nativeAnchorClick=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){window.downloads.push(this.getAttribute('href'));};
window.fetch=async(input,options={})=>{
 const url=new URL(input,location.href),path=url.pathname;let data={};
 if(options.method==='POST'){
  window.commands.push(path);const id=Number(path.split('/')[3]);
  if(id===window.failId)return new Response(JSON.stringify({detail:'Synthetic partial failure'}),{status:409});
  if(path.endsWith('/trash/trash'))trashed.add(id);
  else if(path.endsWith('/trash/restore'))trashed.delete(id);
  else throw Error('Unexpected destructive fixture command '+path);
 }else if(path==='/api/me')data={role:'admin'};
 else if(path==='/api/status')data={job:null,video:null,conversion:null,monitor:{state:'healthy'}};
 else if(path==='/api/storage/settings')data={};
 else if(path==='/api/media'){
  const q=url.searchParams;let list=records.filter(m=>q.get('kind')==='trash'?trashed.has(m.id):!trashed.has(m.id));
  if(q.get('year')!=='all')list=list.filter(m=>m.year===q.get('year'));
  if(q.get('import_job')!=='all')list=list.filter(m=>m.job===Number(q.get('import_job')));
  if(q.get('search'))list=list.filter(m=>m.name.includes(q.get('search')));
  const pages=Math.max(1,Math.ceil(list.length/80)),page=Math.min(Number(q.get('page'))||1,pages);
  data={items:list.slice((page-1)*80,page*80),total:list.length,page,pages,page_size:80,years:[2020,2021],imports:[{id:1,label:'One'},{id:2,label:'Two'}],stats:{total:list.length,bytes:list.length*10}};
 }else throw Error('Unexpected fixture request '+path);
 return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
};`;
const server=createServer((req,res)=>{
 if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end(readFileSync(join(webRoot,'index.html'),'utf8').replace('<head>','<head><script>'+fixture+'</script>'));}
 else if(req.url.startsWith('/web/')&&!req.url.includes('..')){const file=join(webRoot,req.url.slice(5));res.setHeader('Content-Type',extname(file)==='.css'?'text/css':'text/javascript');res.end(readFileSync(file));}
 else if(/^\/api\/media\/\d+\/download/.test(req.url)){const id=Number(req.url.split('/')[3]);res.setHeader('Content-Disposition',`attachment; filename="same [${id}].jpg"`);res.setHeader('Content-Type','application/octet-stream');res.end('fixture original '+id);}
 else if(/^\/api\/media\/\d+\/original/.test(req.url)){const id=Number(req.url.split('/')[3]);if(id===5){res.statusCode=404;res.end();return;}const [w,h]=id===3?[600,1400]:id===4?[1600,400]:[1600,1000];res.setHeader('Content-Type','image/svg+xml');res.end(`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="100%" height="100%" fill="#57836b"/><circle cx="${w/2}" cy="${h/2}" r="150" fill="#f9dda0"/></svg>`);}
 else if(req.url.startsWith('/api/media/')){res.setHeader('Content-Type','image/png');res.end(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64'));}
 else{res.statusCode=404;res.end();}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const url=`http://127.0.0.1:${server.address().port}/`,profile=join(out,'profile');
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1280,900',url],{windowsHide:true,stdio:'ignore'});
let ws;const checks=[];
try{
 for(let n=0;n<150&&!existsSync(join(profile,'DevToolsActivePort'));n++)await pause(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 let pages;for(let n=0;n<100;n++){pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();if(pages.some(p=>p.url===url))break;await pause(100);}
 ws=new WebSocket(pages.find(p=>p.url===url).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let counter=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++counter;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text+': '+r.exceptionDetails.exception?.description);return r.result.value;};
 const wait=async expression=>{for(let n=0;n<100;n++){if(await evaluate(expression))return;await pause(50);}throw Error('Timed out: '+expression);};
 const run=expression=>evaluate(expression);
 const count=()=>evaluate("document.querySelectorAll('.tile.selected').length");
 const click=action=>run(`document.querySelector('#trashSelection [data-action="${action}"]').click()`);
 const rect=async index=>evaluate(`(()=>{const r=document.querySelectorAll('#grid .tile')[${index}].getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()`);
 const mouse=(type,x,y,extra={})=>cdp('Input.dispatchMouseEvent',{type,x,y,button:'left',buttons:type==='mouseReleased'?0:1,clickCount:1,...extra});
 const drag=async(a,b,modifiers=0)=>{await mouse('mousePressed',a.x,a.y,{modifiers});for(let n=1;n<=8;n++)await mouse('mouseMoved',a.x+(b.x-a.x)*n/8,a.y+(b.y-a.y)*n/8,{modifiers});const box=await evaluate("(()=>{const r=document.querySelector('.selection-rectangle').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};})()");
  for(const [key,value] of Object.entries({left:Math.min(a.x,b.x),top:Math.min(a.y,b.y),right:Math.max(a.x,b.x),bottom:Math.max(a.y,b.y)}))assert(Math.abs(box[key]-value)<2,`Marquee ${key}: ${box[key]} != ${value}`);
  await mouse('mouseReleased',b.x,b.y,{modifiers});};
 await wait("document.querySelectorAll('#grid .tile').length===80");
 const open=async i=>{await run(`document.querySelectorAll('#grid .tile')[${i}].click()`);await wait("document.querySelector('#viewer').open");};
 const loaded=()=>wait("!document.querySelector('.photo-zoom-controls').hidden");
 const zoom=()=>evaluate("Number(document.querySelector('#fullMedia').dataset.zoom)");
 const key=async(k,modifiers=0)=>{await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:k,modifiers});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:k,modifiers});};
 for(const i of [0,2,3]){
  await open(i);await loaded();assert.equal(await zoom(),1);
  await run("document.querySelector('#photoZoomIn').click()");assert.equal(await zoom(),1.5);
  await mouse('mousePressed',550,420);await mouse('mouseMoved',850,620);await mouse('mouseReleased',850,620);
  assert(await evaluate("!document.querySelector('.photo-viewport').style.transform.startsWith('translate(0px,0px)')"));
  await key('+');assert.equal(await zoom(),2.25);await key('ArrowRight',8);assert.equal(await zoom(),2.25);
  await key('0');assert.equal(await zoom(),1);
  for(let j=0;j<12;j++)await key('+');assert.equal(await zoom(),8);
  await key('+',2);assert.equal(await zoom(),8,'Browser Ctrl+plus not consumed');
  await run("document.querySelector('#closeViewer').click()");await open(i);await loaded();assert.equal(await zoom(),1);
  await run("document.querySelector('#closeViewer').click()");
 }
 checks.push('Landscape, portrait, panorama: mouse pan, keyboard zoom/reset/pan, 8x limit and reopen reset');
 await open(0);await loaded();await key('+');await key('ArrowRight');await wait("!!document.querySelector('#fullMedia video')");assert(await evaluate("document.querySelector('.photo-zoom-controls').hidden"));
 await key('ArrowRight');await loaded();assert.equal(await zoom(),1);await run("document.querySelector('#photoZoomIn').focus()");await key('+');
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await cdp('Emulation.setTouchEmulationEnabled',{enabled:true});await pause(150);
 assert.equal(await zoom(),1.5);
 const touch=(type,points)=>cdp('Input.dispatchTouchEvent',{type,touchPoints:points.map(([x,y,id])=>({x,y,id,radiusX:2,radiusY:2}))});
 await touch('touchStart',[[160,350,1],[230,350,2]]);await touch('touchMove',[[120,350,1],[270,350,2]]);await touch('touchEnd',[]);assert((await zoom())>2,'Pinch zoom within explicitly zoomed photo');
 await touch('touchStart',[[190,350,1]]);await touch('touchMove',[[190,500,1]]);await touch('touchEnd',[]);
 await run("document.querySelector('#photoZoomFit').click()");assert.equal(await zoom(),1);assert.notEqual(await evaluate("getComputedStyle(document.querySelector('#fullMedia')).touchAction"),'none');
 checks.push('Navigation/video reset and hidden controls; resize; actual touch pinch/pan; fit restores browser gestures');
 await run("document.querySelector('#closeViewer').click()");await open(4);await wait("document.querySelector('.viewer-message').textContent==='Preview unavailable'");assert(await evaluate("document.querySelector('.photo-zoom-controls').hidden"));
 checks.push('Failed image hides zoom controls');
 await run("document.querySelector('#closeViewer').click()");await open(0);await loaded();await key('+');
 assert(await evaluate("(()=>{const c=document.querySelector('#captureTime').getBoundingClientRect(),b=document.querySelector('.photo-zoom-controls').getBoundingClientRect();return c.bottom<b.top;})()"),'Caption does not overlap zoom toolbar');
 const screenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'photo-zoom.png'),Buffer.from(screenshot.data,'base64'));
 writeFileSync(join(out,'results.json'),JSON.stringify({ok:true,checks},null,2));console.log(JSON.stringify({ok:true,browser:'Installed Windows Chrome; isolated fixtures',checks,artifacts:out}));
 await cdp('Browser.close').catch(()=>{});
}finally{ws?.close();chrome.kill();server.close();}
