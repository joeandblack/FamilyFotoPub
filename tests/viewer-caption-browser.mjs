import {chromePath} from './chrome.mjs';
// Installed Windows Chrome + real CDP mouse/touch events. All API data/actions
// are disposable fixtures; no production backend or photo data is accessed.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync,mkdirSync} from 'node:fs';
import {resolve,join,extname} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=resolve('.'),webRoot=resolve(process.env.FAMILYFOTO_WEB_ROOT||'web'),out=mkdtempSync(join(root,'temp','viewer-caption-'));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const fixture=`
window.commands=[];window.downloads=[];window.albumWrites=[];window.confirm=()=>true;window.failId=null;
sessionStorage.setItem('automatic-fixture-job','old-test-job');
const records=Array.from({length:160},(_,i)=>({id:i+1,kind:i===1?'video':'image',captured_at:'2026-10-14T12:34:56',name:'same.jpg',size:10,width:10,height:10,relative_path:'fixture.jpg',sha256:String(i).padStart(64,'0'),year:i%2?'2020':'2021',job:i%3?1:2}));
const trashed=new Set();
window.nativeAnchorClick=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){window.downloads.push(this.getAttribute('href'));};
window.fetch=async(input,options={})=>{
 const url=new URL(input,location.href),path=url.pathname;let data={};
 if(options.method==='POST'){
  window.commands.push(path);const id=Number(path.split('/')[3]);
  if(id===window.failId)return new Response(JSON.stringify({detail:'Synthetic partial failure'}),{status:409});
  if(path==='/api/albums'||path==='/api/albums/9/items'){window.albumWrites.push({path,body:JSON.parse(options.body)});data={id:10};}
  else if(path.endsWith('/trash/trash'))trashed.add(id);
  else if(path.endsWith('/trash/restore'))trashed.delete(id);
  else throw Error('Unexpected destructive fixture command '+path);
 }else if(path==='/api/me')data={role:'admin',capabilities:{people_preview:true}};
 else if(path==='/api/people-preview/groups/1/photos')data={items:[records[0],records[2],records[4]]};
 else if(path==='/api/albums')data={albums:[{id:9,name:'Existing collection'}]};
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
 if(req.url.startsWith('/people-preview')){res.setHeader('Content-Type','text/html');res.end(readFileSync(join(webRoot,'automatic-people-preview.html')));return;}
 if(req.url==='/api/people-preview/jobs/import-20'){res.statusCode=404;res.setHeader('Content-Type','application/json');res.end(JSON.stringify({detail:'Job not found'}));return;}
 if(req.url.startsWith('/api/people-preview')){res.setHeader('Content-Type','application/json');const selections=req.url.endsWith('/selections');const data=selections?[{id:'year:2004',label:'2004 - 5 pilot photos'},{id:'import:14',label:'Import 14'},{id:'import:20',label:'Import 20'}]:req.url.includes('/jobs/')?{id:'import-14',state:'completed',done:3,total:3}:{groups:[{id:1,person_id:1,name:'Fixture',faces:[{detection_id:1,media_id:3}],optional_outlier:false}],revision:1,outlier_faces:0};setTimeout(()=>res.end(JSON.stringify(data)),selections?150:0);return;}
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
 await wait("document.querySelectorAll('#grid .tile').length===80");
 assert.equal(await evaluate("document.querySelector('#grid .tile').dataset.filename"),'same.jpg');
 const hoverTile=await rect(0);
 await mouse('mouseMoved',hoverTile.x+20,hoverTile.y+20,{button:'none',buttons:0});
 await wait("!document.getElementById('galleryFilenameTooltip').hidden");
 assert.equal(await evaluate("document.getElementById('galleryFilenameTooltip').textContent"),'same.jpg');
 await mouse('mouseMoved',-1,-1,{button:'none',buttons:0});
 await wait("document.getElementById('galleryFilenameTooltip').hidden");
 checks.push('Thumbnail hover tooltip includes filename and extension');
 const opacity=()=>evaluate("getComputedStyle(document.getElementById('captureTime')).opacity");
 for(const index of [0,1]){
  const tile=await rect(index);
  await mouse('mousePressed',tile.x+20,tile.y+20);await mouse('mouseReleased',tile.x+20,tile.y+20);
  await wait("document.getElementById('viewer').open");
  assert.equal(await evaluate("!!document.querySelector('#fullMedia video')"),index===1);
  await mouse('mouseMoved',-1,-1,{button:'none',buttons:0});
  assert.equal(await opacity(),'0','Caption hidden off viewer');
  const before=await evaluate("document.getElementById('fullMedia').getBoundingClientRect().toJSON()");
  await mouse('mouseMoved',500,400,{button:'none',buttons:0});
  assert.equal(await opacity(),'1','Caption visible on viewer hover');
  assert.deepEqual(await evaluate("document.getElementById('fullMedia').getBoundingClientRect().toJSON()"),before);
  await mouse('mouseMoved',-1,-1,{button:'none',buttons:0});assert.equal(await opacity(),'0');
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  assert.equal(await opacity(),'1','Keyboard focus reveals caption');
  await run("document.getElementById('viewerInfo').click()");
  assert(await evaluate("document.getElementById('mediaDetails').textContent.includes('2026-10-14 12:34:56')"));
  if(index===1){
    const hit=async()=>{
      const r=await evaluate("document.querySelector('.video-controls button:last-child').getBoundingClientRect().toJSON()");
      await mouse('mousePressed',r.x+r.width/2,r.y+r.height/2);await mouse('mouseReleased',r.x+r.width/2,r.y+r.height/2);
    };
    await hit();
    await wait("document.fullscreenElement?.classList.contains('viewer-surface')");
    assert(await evaluate("document.fullscreenElement.contains(document.querySelector('#fullMedia video')) && document.fullscreenElement.contains(document.querySelector('.video-controls')) && !document.fullscreenElement.contains(document.querySelector('.layout'))"));
    const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'video-fullscreen.png'),Buffer.from(shot.data,'base64'));
    await hit();await wait('!document.fullscreenElement');
    checks.push('Fullscreen targets only viewer surface with video and controls; second click exits');
  }
  await run("document.getElementById('closeViewer').click()");
  checks.push((index?'Video':'Photo')+' caption hides, shows on hover/keyboard without layout shift; information retains timestamp');
 }
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await cdp('Emulation.setTouchEmulationEnabled',{enabled:true});
 for(const index of [0,1]){
  await run("document.getElementById('grid').scrollTop=0");
  const tile=await rect(index);
  await cdp('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:tile.x+20,y:tile.y+20}]});
  await cdp('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await wait("document.getElementById('viewer').open");
  assert.equal(await opacity(),'0','Touch open must not reveal caption');
  await cdp('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:190,y:400}]});
  await cdp('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  assert.equal(await opacity(),'0','Touch tap must not leave sticky hover');
  await run("document.getElementById('viewerInfo').click()");
  assert(await evaluate("!document.getElementById('viewerDetails').hidden&&document.getElementById('mediaDetails').textContent.includes('2026-10-14 12:34:56')"));
  await run("document.getElementById('closeViewer').click()");
 }
 checks.push('Mobile photo/video tap does not leave caption visible; information available');
 await pause(300);
 await run("document.querySelector('#grid .tile').click()");
 await wait("document.getElementById('viewer').open");
 await run("window.failId=Number(document.querySelector('#grid .tile').dataset.id);document.getElementById('viewerTrash').click()");
 await wait("document.getElementById('captureTime').dataset.error==='true'");
 assert.equal(await opacity(),'1','Action errors remain visible without hover');
 checks.push('Viewer action errors remain visible on touch');
 await run("document.getElementById('closeViewer').click();document.querySelector('[data-view=\"people-preview\"]').click();document.querySelector('iframe').src='/people-preview?desktop=1&jobs=1&integrated=1&import=14'");
 await wait("!!document.querySelector('iframe[title=\"People preview\"]').contentDocument.querySelector('.open-person-photo')");
 await wait("document.querySelector('iframe[title=\"People preview\"]').contentDocument.querySelector('#job-selection').options.length===1");
 assert(await evaluate("(()=>{const d=document.querySelector('iframe[title=\"People preview\"]').contentDocument,s=d.querySelector('#job-selection');return s.value==='import:14'&&s.disabled&&s.selectedOptions[0].textContent==='Import #14 · 3 photos'&&d.querySelector('#job-start').disabled&&sessionStorage.getItem('automatic-fixture-job')===null})()"));
 checks.push('Completed import selection stays synchronized after delayed choices; legacy test selection cleared and Start disabled');
 await run("document.querySelector('iframe[title=\"People preview\"]').contentDocument.querySelector('.open-person-photo').click()");
 await wait("document.getElementById('viewer').open");
 assert(await evaluate("document.querySelector('#fullMedia img').src.includes('/api/media/3/original')"));
 await run("document.getElementById('viewerNext').click()");
 assert(await evaluate("document.querySelector('#fullMedia img').src.includes('/api/media/5/original')&&document.getElementById('viewerNext').disabled"));
 await run("document.getElementById('viewerPrevious').click();document.getElementById('viewerPrevious').click()");
 assert(await evaluate("document.querySelector('#fullMedia img').src.includes('/api/media/1/original')&&document.getElementById('viewerPrevious').disabled"));
 await run("document.getElementById('closeViewer').click()");
 assert(await evaluate("!document.querySelector('iframe[title=\"People preview\"]').hidden"));
 checks.push('People photos open at selected image, navigate only group photos and close back to People');
 for(const target of ['9','new']){
  await run("document.querySelector('iframe[title=\"People preview\"]').contentDocument.querySelector('.add-group-album').click()");
  await wait("document.getElementById('albumDialog').open&&!document.getElementById('albumSave').disabled");
  await run(`document.getElementById('albumTarget').value='${target}';document.getElementById('albumTarget').dispatchEvent(new Event('change'));document.getElementById('albumName').value='Person photos';document.getElementById('albumForm').requestSubmit()`);
  await wait("!document.getElementById('albumDialog').open");
 }
 const writes=await evaluate('window.albumWrites');assert.equal(writes.length,2);
 assert.deepEqual(writes.map(w=>w.body.media_ids),[[1,3,5],[1,3,5]]);
 assert.equal(writes[0].path,'/api/albums/9/items');assert.equal(writes[1].path,'/api/albums');assert.equal(writes[1].body.name,'Person photos');
 checks.push('People group adds all unique photos to existing or newly created album');
 await run("document.querySelector('iframe').src='/people-preview?desktop=1&jobs=1&integrated=1'");
 await wait("document.querySelector('iframe').contentDocument.querySelector('#job-selection')?.options.length===2");
 assert(await evaluate("document.querySelector('nav [data-view=people]').hidden&&document.querySelector('nav [data-view=faces]').hidden"));
 await run("(()=>{const s=document.querySelector('iframe').contentDocument.querySelector('#job-selection');s.value='import:20';s.dispatchEvent(new Event('change'));})()");
 await wait("document.querySelector('iframe').contentDocument.querySelector('#job-start').disabled===false");
 await run("(()=>{const s=document.querySelector('iframe').contentDocument.querySelector('#job-selection');s.value='import:14';s.dispatchEvent(new Event('change'));})()");
 await wait("document.querySelector('iframe').contentDocument.querySelector('#job-controls').dataset.state==='completed'");
 assert(await evaluate("document.querySelector('iframe').contentDocument.querySelector('#job-start').disabled"));
 checks.push('People hides legacy pilots; unscanned imports enable Start and completed imports disable it when switching');
 const screenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'viewer-caption.png'),Buffer.from(screenshot.data,'base64'));
 writeFileSync(join(out,'results.json'),JSON.stringify({ok:true,checks},null,2));console.log(JSON.stringify({ok:true,browser:'Installed Windows Chrome, real CDP pointer events, synthetic backend',checks,artifacts:out}));
 await cdp('Browser.close').catch(()=>{});
}finally{ws?.close();chrome.kill();server.close();}
