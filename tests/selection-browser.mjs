import {chromePath} from './chrome.mjs';
// Installed Windows Chrome + real CDP mouse/touch events. All API data/actions
// are disposable fixtures; no production backend or photo data is accessed.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync,mkdirSync} from 'node:fs';
import {resolve,join,extname} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=resolve('.'),webRoot=resolve(process.env.FAMILYFOTO_WEB_ROOT||'web'),out=mkdtempSync(join(root,'temp','selection-browser-'));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const fixture=`
window.commands=[];window.downloads=[];window.confirm=()=>true;window.failId=null;
const records=Array.from({length:160},(_,i)=>({id:i+1,kind:'image',name:'same.jpg',size:10,width:10,height:10,relative_path:'fixture.jpg',sha256:String(i).padStart(64,'0'),year:i%2?'2020':'2021',job:i%3?1:2}));
const trashed=new Set();
records[0]={...records[0],kind:'video',name:'Music.mp3',video_metadata:{media_type:'audio',duration_seconds:216}};
records[1]={...records[1],kind:'video',name:'Misnamed.mp4',video_metadata:{media_type:'audio',duration_seconds:216}};
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
 await wait("document.querySelectorAll('#grid .tile').length===80");await click('select');
 assert.equal(await evaluate("document.querySelectorAll('#grid .audio-tile').length"),2);
 assert.equal(await evaluate("document.querySelectorAll('#grid .audio-placeholder svg').length"),2);
 assert(await evaluate("document.querySelector('.library-summary-line .selection-count').hidden"));
 assert.equal(await evaluate("document.querySelector('.selection-count').textContent"),'');
 assert(await evaluate("document.querySelector('#trashSelection').lastElementChild.classList.contains('selection-result')"));
 const albumBefore=await evaluate("document.querySelector('#trashSelection [data-action=album]').getBoundingClientRect().top");
 await run("document.querySelector('.selection-result').textContent='Operation complete.'");
 assert.equal(await evaluate("document.querySelector('#trashSelection [data-action=album]').getBoundingClientRect().top"),albumBefore);
 await run("document.querySelector('.selection-result').textContent=''");
 const first=await rect(0),second=await rect(1),third=await rect(2);
 await run("(()=>{const range=document.createRange();range.selectNodeContents(document.getElementById('grid'));window.getSelection().addRange(range);})()");
 await mouse('mousePressed',first.x+20,first.y+20);await mouse('mouseReleased',first.x+20,first.y+20);
 assert(await evaluate("!document.querySelector('.library-summary-line .selection-count').hidden"));
 assert.equal(await evaluate("document.querySelector('.library-summary-line .selection-count').previousElementSibling.id"),'summary');
 assert.equal(await evaluate('window.getSelection().rangeCount'),0);assert.equal(await count(),1);assert.equal(await evaluate("document.getElementById('viewer').open"),false);
 await mouse('mousePressed',third.x+20,third.y+20,{modifiers:8});await mouse('mouseReleased',third.x+20,third.y+20,{modifiers:8});assert.equal(await count(),3);
 await click('clear');await drag({x:first.x+2,y:first.y+2},{x:second.x+second.w-2,y:second.y+second.h-2});assert.equal(await count(),2);
 await drag({x:first.x+2,y:first.y+2},{x:first.x+first.w-2,y:first.y+first.h-2},2);assert.equal(await count(),1);
 checks.push('Native text/image range is cleared; real mouse/shift/rectangle/control-toggle never opens viewer');

 // Mid-grid starts must not borrow the first row or a previous click anchor.
 for(const scrolled of [false,true]){
  await run(`document.getElementById('grid').scrollTop=${scrolled?170:0}`);await pause(50);
  const start=await rect(scrolled?25:18),end=await rect(scrolled?34:27);
  const corners=[{x:start.x+8,y:start.y+8},{x:end.x+end.w-8,y:end.y+end.h-8}];
  for(const reverseX of [false,true])for(const reverseY of [false,true]){
   await click('clear');
   const a={x:corners[reverseX?1:0].x,y:corners[reverseY?1:0].y},b={x:corners[reverseX?0:1].x,y:corners[reverseY?0:1].y};
   const expected=await evaluate(`(()=>{const a=${JSON.stringify(a)},b=${JSON.stringify(b)};return [...document.querySelectorAll('#grid .tile')].filter(t=>{const r=t.getBoundingClientRect();return r.left<Math.max(a.x,b.x)&&r.right>Math.min(a.x,b.x)&&r.top<Math.max(a.y,b.y)&&r.bottom>Math.min(a.y,b.y)}).map(t=>Number(t.dataset.id));})()`);
   await drag(a,b);
   assert.deepEqual(await evaluate("[...document.querySelectorAll('.tile.selected')].map(t=>Number(t.dataset.id))"),expected,`Scrolled=${scrolled},reverseX=${reverseX},reverseY=${reverseY}`);
  }
 }
 await run("document.getElementById('grid').scrollTop=0");await pause(50);
 checks.push('Marquee bounds and selected cards match mid-grid/scrolled drags in all four directions');

 await click('clear');await mouse('mousePressed',first.x+2,first.y+2);
 const bottom=await evaluate("document.getElementById('grid').getBoundingClientRect().bottom-3");
 await mouse('mouseMoved',second.x+20,bottom);await pause(350);assert(await evaluate("document.getElementById('grid').scrollTop>0"));await mouse('mouseReleased',second.x+20,bottom);
 assert(await count()>2);checks.push('Drag edge autoscroll keeps selection in content coordinates');
 await click('clear');await run("document.getElementById('grid').scrollTop=0");await pause(50);
 await cdp('Emulation.setTouchEmulationEnabled',{enabled:true});
 await cdp('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:first.x+20,y:first.y+100}]});
 for(let i=1;i<=5;i++)await cdp('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:first.x+20,y:first.y+100-i*15}]});
 await cdp('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await wait("document.getElementById('grid').scrollTop>0");assert.equal(await count(),0);
 await cdp('Emulation.setTouchEmulationEnabled',{enabled:false});checks.push('Touch scrolling does not start rectangle selection');
 await click('all');assert.equal(await count(),80);
 await run("document.getElementById('next').click()");await wait("document.querySelector('#grid .tile')?.dataset.id==='81'");assert.equal(await count(),0);
 await run("document.getElementById('previous').click()");await wait("document.querySelector('#grid .tile')?.dataset.id==='1'");
 checks.push('Paging clears selections instead of selecting unseen results');
 await run("document.getElementById('year').value='2020';document.getElementById('year').dispatchEvent(new Event('change'))");
 await wait("document.querySelectorAll('#grid .tile').length===80&&document.querySelector('#trashSelection [data-action=select]').textContent==='Select'&&!document.querySelector('#trashSelection [data-action=select]').disabled");
 await click('select');await click('all');assert.equal(await count(),80);
 await run("document.getElementById('importFilter').value='2';document.getElementById('importFilter').dispatchEvent(new Event('change'))");
 await wait("document.querySelectorAll('#grid .tile').length===27");assert.equal(await count(),0);
 await click('select');await run("document.querySelectorAll('#grid .tile')[0].click()");await click('download');
 const links=await evaluate('window.downloads');assert.equal(links.length,1);assert(links[0].endsWith('/download'));
 await run("document.querySelectorAll('#grid .tile')[1].click()");
 checks.push('Year/import filters clear selection; single-item download stays direct (multi-file archive covered by real API browser test)');

 const selected=await evaluate("[...document.querySelectorAll('.tile.selected')].map(e=>Number(e.dataset.id))");await run(`window.failId=${selected[1]}`);await click('delete');
 await wait("document.querySelector('.selection-result').textContent.includes('Stopped:')");
 assert.equal(await evaluate("document.querySelectorAll('.confirm-dialog[open]').length"),0);
 assert.deepEqual(await evaluate('window.commands'),selected.map(id=>`/api/media/${id}/trash/trash`));assert.equal(await count(),1);
 checks.push('Recoverable bulk Trash stops on partial failure and keeps remaining selection');
 await wait("!document.querySelector('#trashSelection [data-action=undo]').disabled");
 await run(`window.failId=${selected[0]}`);await click('undo');
 await wait("document.querySelector('.selection-result').textContent.includes('0 restored. Stopped:')");
 assert(!await evaluate("document.querySelector('#trashSelection [data-action=undo]').disabled"));
 await run('window.failId=null');await click('undo');
 await wait("document.querySelectorAll('#grid .tile').length===27&&document.querySelector('#trashSelection [data-action=undo]').disabled");
 assert(await evaluate(`window.commands.includes('/api/media/${selected[0]}/trash/restore')`));
 await click('clear');await run(`document.querySelector('#grid [data-id="${selected[0]}"]').click()`);await click('delete');
 await wait("document.querySelectorAll('#grid .tile').length===26&&!document.querySelector('#trashSelection [data-action=undo]').disabled");
 checks.push('Trash needs no confirmation; Undo restores only the last successful batch and retries failed restores');
 await run("document.querySelector('[data-view=trash]').click()");await wait("document.body.dataset.view==='trash'&&document.querySelectorAll('#grid .tile').length===1");await click('select');
 assert.equal(await evaluate("document.querySelector('#trashSelection [data-action=delete]').textContent"),'Delete permanently');
 await click('all');const beforePermanent=await evaluate('window.commands.length');await click('delete');
 await wait("!!document.querySelector('.confirm-dialog[open]')");
 assert.equal(await evaluate('window.commands.length'),beforePermanent);
 await run("document.querySelector('.confirm-dialog-actions button:first-child').click()");
 await run("document.body.dataset.role='regular'");assert.equal(await evaluate("getComputedStyle(document.querySelector('#trashSelection [data-action=delete]')).display"),'none');
 await run("document.querySelector('[data-view=all]').click()");await wait("document.body.dataset.view==='all'&&document.querySelectorAll('#grid .tile').length===26");await click('select');
 assert.notEqual(await evaluate("getComputedStyle(document.querySelector('#trashSelection [data-action=delete]')).display"),'none');
 await run("document.body.dataset.role='guest'");assert.equal(await evaluate("getComputedStyle(document.querySelector('#trashSelection [data-action=delete]')).display"),'none');
 checks.push('Recently Deleted permanent-delete and regular/guest visibility remain distinct');
 await run("document.body.dataset.role='admin';document.querySelector('#grid .tile').focus()");
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',modifiers:2});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',modifiers:2});assert.equal(await count(),26);
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});assert.equal(await count(),0);
 checks.push('Keyboard Select page and Escape clear selection');
 const screenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'selection.png'),Buffer.from(screenshot.data,'base64'));
 writeFileSync(join(out,'results.json'),JSON.stringify({ok:true,checks},null,2));console.log(JSON.stringify({ok:true,browser:'Installed Windows Chrome, real CDP pointer events, synthetic backend',checks,artifacts:out}));
 await cdp('Browser.close').catch(()=>{});
}finally{ws?.close();chrome.kill();server.close();}
