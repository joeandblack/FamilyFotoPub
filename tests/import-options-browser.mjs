import {chromePath} from './chrome.mjs';
// Installed Windows Chrome + real CDP mouse/touch events. All API data/actions
// are disposable fixtures; no production backend or photo data is accessed.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync,mkdirSync} from 'node:fs';
import {resolve,join,extname} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const root=resolve('.'),webRoot=resolve(process.env.FAMILYFOTO_WEB_ROOT||'web'),out=mkdtempSync(join(root,'temp','import-options-'));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const fixture="// Loaded before app.js in the disposable Chrome smoke-test page.\nconst batches=[1,2].map(id=>({id,source_root:`Fixture/photos/import-${id}`,\n started_at:'2026-10-13T00:00:00Z',state:'completed-review-required',total:4,total_bytes:4000,\n counts:{imported:2,duplicate:1,pending:1},bytes_by_state:{imported:2000,duplicate:1000,pending:1000},\n stages:{metadata:{done:1,pending:1},conversion:{pending:2}},auto_metadata:false,auto_conversion:false}));\nbatches.push({...batches[0],id:3,source_root:'Fixture/photos/duplicates-only',counts:{duplicate:4},stages:{metadata:{},conversion:{}}});\nlet pipeline={busy:false,job_id:2,stage:'metadata',pause_requested:false};\nconst commands=[],failures=[];\nlet photoTrashed=false;\nconst latestSource=batches.at(-1).source_root;\nlet storageConfig={source:'F:\\\\PHOTOS\\\\Fixture',destination:'E:\\\\FamilyFoto\\\\media',destination_locked:true,revision:'fixture-1'};\nconst photo={id:900001,kind:'image',name:'Disposable fixture.jpg',size:100,width:20,height:20,sha256:'a'.repeat(64),relative_path:'fixture.jpg'};\nwindow.addEventListener('error',e=>failures.push(e.message));\nwindow.addEventListener('unhandledrejection',e=>failures.push(String(e.reason)));\nwindow.fetch=async(input,options={})=>{\n const path=new URL(input,location.href).pathname;\n let data;\n if(options.method==='POST'){\n  commands.push({path,body:options.body?JSON.parse(options.body):null});\n  if(path==='/api/import/start')data={message:'Started fixture'};\n  else if(path==='/api/import/2/pause')pipeline.pause_requested=true;\n  else if(path==='/api/import/1/options')Object.assign(batches[0],JSON.parse(options.body));\n  else if(path==='/api/media/900001/trash/trash')photoTrashed=true;\n  else if(path==='/api/media/900001/trash/restore')photoTrashed=false;\n  else if(path==='/api/storage/settings'){const value=JSON.parse(options.body);check(!pipeline.busy,'No storage save during work');check(value.destination===storageConfig.destination,'Populated destination unchanged');storageConfig={...storageConfig,source:value.source,revision:'fixture-2'};}\n  else throw Error('Unexpected fixture command '+path);\n  data={message:'Fixture action recorded'};\n }else if(path==='/api/me')data={username:'fixture',role:'admin'};\n else if(path==='/api/storage/settings')data={...storageConfig,latest_source:latestSource,busy:pipeline.busy};\n else if(path==='/api/storage')data={checked_at:new Date().toISOString(),source:{root:latestSource,serial:'fixture-source',volume_serial:'fixture',total:1000,used:100,free:900},destination:{root:storageConfig.destination,serial:'fixture-destination',volume_serial:'fixture',total:2000,used:1000,free:1000}};\n else if(path==='/api/status')data={job:null,video:null,conversion:null,monitor:{state:'healthy',updated:new Date().toISOString()}};\n else if(path==='/api/import/history')data={imports:batches,pipeline};\n else if(path==='/api/issues')data={imports:[{job_id:2,errors:[{source_path:'Fixture/photos/import-2/failed.jpg',error:'Fixture copy error'}],unsupported:[{extension:'.xyz',count:2,files:[{source_path:'Fixture/photos/import-2/one.xyz'},{source_path:'Fixture/photos/import-2/two.xyz'}]}]}]};\n else if(path==='/api/import/ignore-patterns')data={patterns:['*.xmp','*.srt']};\n else if(path==='/api/media'){\n  const kind=new URL(input,location.href).searchParams.get('kind');\n  const items=(kind==='trash'?photoTrashed:kind!=='video'&&!photoTrashed)?[{...photo,trashed_at:photoTrashed?'2026-10-13T00:00:00Z':null}]:[];\n  data={items,total:items.length,page:1,pages:1,page_size:1000,years:[],stats:{total:items.length,bytes:items.length*100}};\n }\n else throw Error('Unexpected fixture request '+path);\n return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});\n};\nconst wait=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(resolve=>setTimeout(resolve,20));}throw Error('Timed out waiting for fixture UI');};\nconst check=(value,message)=>{if(!value)throw Error(message);};\nconst group=id=>document.querySelector(`.import-group[data-job=\"${id}\"]`);\nconst buttons=id=>[...group(id).querySelectorAll('.pipeline-heading button')];\n\nwindow.confirm=()=>true;";
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
 const confirmImport=async()=>{await wait("document.querySelector('.confirm-dialog[open] .btn-primary')");await run("document.querySelector('.confirm-dialog[open] .btn-primary').click()");};
 const count=()=>evaluate("document.querySelectorAll('.tile.selected').length");
 const click=action=>run(`document.querySelector('#trashSelection [data-action="${action}"]').click()`);
 const rect=async index=>evaluate(`(()=>{const r=document.querySelectorAll('#grid .tile')[${index}].getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()`);
 const mouse=(type,x,y,extra={})=>cdp('Input.dispatchMouseEvent',{type,x,y,button:'left',buttons:type==='mouseReleased'?0:1,clickCount:1,...extra});
 const drag=async(a,b,modifiers=0)=>{await mouse('mousePressed',a.x,a.y,{modifiers});for(let n=1;n<=8;n++)await mouse('mouseMoved',a.x+(b.x-a.x)*n/8,a.y+(b.y-a.y)*n/8,{modifiers});const box=await evaluate("(()=>{const r=document.querySelector('.selection-rectangle').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom};})()");
  for(const [key,value] of Object.entries({left:Math.min(a.x,b.x),top:Math.min(a.y,b.y),right:Math.max(a.x,b.x),bottom:Math.max(a.y,b.y)}))assert(Math.abs(box[key]-value)<2,`Marquee ${key}: ${box[key]} != ${value}`);
  await mouse('mouseReleased',b.x,b.y,{modifiers});};
 await wait("document.body?.dataset.role==='admin'");await run("document.querySelector('[data-view=imports]').click()");await wait("document.querySelector('.import-group')");
 const checked=selector=>evaluate(`document.querySelector('${selector}').checked`);
 const point=async(selector,label=false)=>evaluate(`(()=>{let e=document.querySelector('${selector}');if(${label})e=e.closest('label');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+${label?'r.width-10':'r.width/2'},y:r.y+r.height/2};})()`);
 const tap=async(selector,label=false)=>{const p=await point(selector,label);await mouse('mousePressed',p.x,p.y);await mouse('mouseReleased',p.x,p.y);};
 const space=async()=>{await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:' ',code:'Space',windowsVirtualKeyCode:32});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:' ',code:'Space',windowsVirtualKeyCode:32});};
 const a='#newAutoMetadata',b='#newAutoConversion';
 assert.equal(await evaluate("document.getElementById('importDuplicateMode')===null"),true);
 assert.equal(await run("document.querySelector('#manualImport details').open"),false);
 assert.equal(await checked(a),true);
 await run("document.querySelector('#manualImport details').open=true");
 await tap(b,true);assert.equal(await checked(b),true);
 await tap('#newAutoDuplicates',true);
 assert.equal(await evaluate("getComputedStyle(document.querySelector('#newAutoMetadata')).appearance"),'auto');
 await run("document.querySelector('#importSource').value='D:\\\\Fixtures';document.querySelector('#startImport').click()");
 await confirmImport();await wait("commands.some(c=>c.path==='/api/import/start')");assert.deepEqual(await evaluate("commands.filter(c=>c.path==='/api/import/start').at(-1).body"),{source:'D:\\Fixtures',auto_metadata:true,auto_conversion:true,auto_duplicates:true});
 await wait("!document.querySelector('#manualImport details').open");
 assert.equal(await run("document.querySelector('#startImport').disabled"),true);
 await run("pipeline.busy=true;document.querySelector('#importHistory').dispatchEvent(new CustomEvent('pipeline-status',{detail:pipeline}))");
 assert.equal(await run("document.querySelector('#startImport').disabled"),true);
 await run("pipeline.busy=false;document.querySelector('#importHistory').dispatchEvent(new CustomEvent('pipeline-status',{detail:pipeline}))");
 await wait("!document.querySelector('#startImport').disabled");
 await run("document.querySelector('#manualImport details').open=true");
 await tap('#newAutoDuplicates',true);
 await run("document.querySelector('#newAutoMetadata').focus()");await space();await tap(b,true);
 await run("document.querySelector('#importSource').value='D:\\\\Fixtures';document.querySelector('#importSource').dispatchEvent(new Event('input'));document.querySelector('#startImport').click()");await confirmImport();await wait("commands.filter(c=>c.path==='/api/import/start').length===2");assert.deepEqual(await evaluate("commands.filter(c=>c.path==='/api/import/start').at(-1).body"),{source:'D:\\Fixtures',auto_metadata:false,auto_conversion:false,auto_duplicates:false});
 await run("document.querySelector('[data-job=\"1\"]').open=true");
 const gb='[data-job="1"] [data-option="auto_conversion"]';
 assert.equal(await evaluate("document.querySelector('[data-job=\"1\"] [data-option=auto_metadata]')===null"),true);
 await tap(gb,true);await wait("batches[0].auto_conversion && !document.querySelector('[data-job=\"1\"] [data-option=auto_conversion]').disabled");assert.equal(await checked(gb),true);
 await run("document.querySelector('[data-job=\"1\"] [data-option=auto_conversion]').focus()");await space();await wait("!batches[0].auto_conversion && !document.querySelector('[data-job=\"1\"] [data-option=auto_conversion]').disabled");
 assert.equal(await checked(gb),false);
 await run("document.querySelector('#importHistory').dispatchEvent(new CustomEvent('pipeline-status',{detail:{busy:true}}));document.querySelector('#importHistory').dispatchEvent(new CustomEvent('pipeline-status',{detail:{busy:false}}));window.previousFixtureFetch=window.fetch;window.fetch=async(...args)=>new URL(args[0],location.href).pathname==='/api/import/start'?new Response(JSON.stringify({detail:'Import not started: folder path belongs to completed Import #21. Rename it to import different files.'}),{status:409}):window.previousFixtureFetch(...args);document.querySelector('#manualImport details').open=true;document.querySelector('#startImport').click()");
 await confirmImport();await wait("!document.querySelector('#importStartError').hidden");
 assert.match(await run("document.querySelector('#importStartError').textContent"),/Import #21/);
 await run("document.querySelector('#manualImport details').open=false");
 assert.equal(await run("document.querySelector('#importStartError').getBoundingClientRect().height>0"),true);
 await run("document.querySelector('#manualImport details').open=true");
 checks.push('Rejected import has a persistent visible error outside the source disclosure; controls and options pass mouse and keyboard checks');
 const screenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'options.png'),Buffer.from(screenshot.data,'base64'));
 await run("document.querySelector('#manualImport').scrollIntoView({block:'start'})");
 const modeScreenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'import-mode.png'),Buffer.from(modeScreenshot.data,'base64'));
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 await run("document.querySelector('#manualImport').scrollIntoView({block:'start'})");
 const mobileScreenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'import-mode-mobile.png'),Buffer.from(mobileScreenshot.data,'base64'));
 writeFileSync(join(out,'results.json'),JSON.stringify({ok:true,checks},null,2));console.log(JSON.stringify({ok:true,checks,artifacts:out}));
 await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
