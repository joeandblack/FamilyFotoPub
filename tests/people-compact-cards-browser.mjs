import {chromePath} from './chrome.mjs';
// Isolated browser fixture: no production People changes.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join,basename} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(join(resolve('temp'),'compact-people-'));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const groups=[
 {id:1,name:'Named person',person_id:1,section:'people'},
 {id:2,name:null,person_id:null,section:'review'},
 {id:3,name:null,person_id:null,section:'other'}
].map(g=>({...g,review_version:`fixture-${g.id}`,faces:[{detection_id:g.id*10,media_id:g.id*10},{detection_id:g.id*10+1,media_id:g.id*10+1}]}));
let many=false;
const bulkRequests=[];
const streamedRequests=[];
const renameRequests=[];
const nameRequests=[];
let rejectName=true;
const coverRequests=[];
const faceActions=[];
let ignoreAttempts=0;
let catalogRequests=0;
let organized=null;
const scopedCalls=[];
let blurryRequests=0,blurryRestores=0;
const blurryFaces=Array.from({length:101},(_,i)=>({detection_id:90000+i,media_id:90000+i,name:`Long original filename for blurry face ${i}.jpg`,quality:{reason:i%2?'blurry':'too-small'}}));
const server=createServer((req,res)=>{
 const path=new URL(req.url,'http://localhost').pathname;
 const importId=new URL(req.url,'http://localhost').searchParams.get('import_id');
 const project=g=>importId?{...g,faces:g.faces.filter(f=>f.detection_id%2===(Number(importId)===1?0:1))}:g;
 if(path.endsWith('/selections')||path.endsWith('/review-imports')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify([{id:'import:1',label:'Import #1'},{id:'import:2',label:'Import #2'}]));return;}
 if(path.endsWith('/suggestions')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({items:[],total:0,page:1,pages:1}));return;}
 if(path.endsWith('/recheck/stream')){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{scopedCalls.push({kind:'recheck',...JSON.parse(body)});res.setHeader('Content-Type','application/x-ndjson');res.end(JSON.stringify({state:'completed',result:{matched:0,remaining:1,suggestions:0}})+'\n');});return;}
 if(path.endsWith('/auto-organize')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({last_run:organized}));return;}
 if(path.endsWith('/auto-organize/stream')){
  let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
   const action=JSON.parse(body).action;scopedCalls.push({kind:'organize',...JSON.parse(body)});res.setHeader('Content-Type','application/x-ndjson');
   res.write(JSON.stringify({state:'running',phase:'matching',done:1,total:2})+'\n');
   setTimeout(()=>{
    organized={id:123,undone:action==='undo',summary:{matched_faces:1,groups_before:2,groups_after:1}};
    res.end(JSON.stringify({state:'completed',result:action==='undo'?{restored:1,kept_later_changes:0}:{...organized.summary,event_id:123}})+'\n');
   },500);
  });return;
 }
 if(req.method==='POST'&&/\/faces\/\d+\/(correct|ignored)$/.test(path)){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{faceActions.push({path,...JSON.parse(body)});res.setHeader('Content-Type','application/json');if(path.endsWith('/ignored')&&++ignoreAttempts===1){res.writeHead(409);res.end('{"detail":"Fixture conflict; try again"}');}else res.end('{}');});return;}
 if(path.endsWith('/ignored')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({items:[{detection_id:80000,media_id:80000,name:'Ignored original.jpg'}],page:1,pages:1,total:1}));return;}
 if(req.method==='POST'&&path.endsWith('/cover')){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{const data=JSON.parse(body);coverRequests.push(data);groups.find(g=>g.id===Number(path.match(/groups\/(\d+)/)[1])).cover_detection_id=data.detection_id;res.setHeader('Content-Type','application/json');res.end('{}');});return;}
 if(path.endsWith('/blurry')){
  blurryRequests++;const page=Math.min(Number(new URL(req.url,'http://localhost').searchParams.get('page')||1),Math.max(1,Math.ceil(blurryFaces.length/100)));
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify({items:blurryFaces.slice((page-1)*100,page*100),page,pages:Math.max(1,Math.ceil(blurryFaces.length/100)),total:blurryFaces.length}));return;
 }
 if(req.method==='POST'&&/\/blurry\/\d+\/review$/.test(path)){
  blurryRestores++;const id=Number(path.match(/blurry\/(\d+)/)[1]);blurryFaces.splice(blurryFaces.findIndex(f=>f.detection_id===id),1);
  res.setHeader('Content-Type','application/json');res.end('{"released":true}');return;
 }
 if(req.method==='POST'&&path.endsWith('/review-actions/stream')){
  let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
   streamedRequests.push(JSON.parse(body));res.setHeader('Content-Type','application/x-ndjson');
   res.write(JSON.stringify({state:'running',phase:'processing',done:1,total:2})+'\n');
   setTimeout(()=>res.write(JSON.stringify({state:'running',phase:'saving'})+'\n'),800);
   setTimeout(()=>res.end(JSON.stringify({state:'completed',result:{matched:2,remaining_group_ids:[2],updates:{groups:[{...groups[1],review_version:'updated'}],removed_group_ids:[],revision:2,people:[{person_id:1,name:'Named person'}]}}})+'\n'),1200);
  });return;
 }
 if(path==='/small'||path==='/large'){many=false;groups[0].faces=Array.from({length:path==='/small'?11:405},(_,i)=>({detection_id:1000+i,media_id:1000+i}));}
 if(req.method==='POST'&&path.endsWith('/review/set-aside')){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{for(const id of JSON.parse(body).group_ids)groups.find(g=>g.id===id).section='other';res.setHeader('Content-Type','application/json');res.end('{"moved":1}');});return;}
 if(path==='/api/people-preview')catalogRequests++;
 const delayedGroup=path.match(/\/groups\/(\d+)$/);
 if(req.method==='GET'&&delayedGroup){setTimeout(()=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({group:project(groups.find(g=>g.id===Number(delayedGroup[1]))),revision:1,history:[{id:1,action:'correct',detection_id:20,actor:'fixture',can_undo:true}]}));},300);return;}
 if(req.method==='POST'&&/\/people\/\d+\/rename$/.test(path)){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{renameRequests.push(JSON.parse(body));res.setHeader('Content-Type','application/json');res.end('{}');});return;}
 if(req.method==='POST'&&/\/groups\/\d+\/name$/.test(path)){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{nameRequests.push({path,...JSON.parse(body)});res.setHeader('Content-Type','application/json');if(rejectName){rejectName=false;res.writeHead(409);res.end('{"detail":"Name already exists"}');}else res.end('{}');});return;}
 if(req.method==='POST'&&path.endsWith('/selected-faces')){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{bulkRequests.push(JSON.parse(body));if(bulkRequests.at(-1).action==='review-later')groups.find(g=>g.id===Number(path.match(/groups\/(\d+)/)[1])).section='other';res.setHeader('Content-Type','application/json');res.end(JSON.stringify({changed:bulkRequests.at(-1).detection_ids.length}));});return;}
 if(path==='/many')many=true;
 if(path==='/collection-fixture'){
  res.setHeader('Content-Type','text/html');res.end(`<link rel="stylesheet" href="/web/styles.css"><main><header><div><h2 id="heading">Albums</h2><p id="summary"></p></div><input id="search"></header><section id="library"></section></main><script src="/web/lucide.min.js"></script><script type="module">
  import {setupAlbums} from '/web/albums.mjs';
  import {createPeoplePhotoGallery} from '/web/people-photo-gallery.mjs';
  document.body.dataset.role='admin';
  window.namedPeople=[{person_id:7,name:'Live person',photo_count:2,cover_detection_id:1}];
  const pilot='a'.repeat(32);
  const catalog=[{id:1,name:'Summer',description:'Summer holiday',item_count:4,cover_id:1},{id:2,name:'Family',description:'Family photos',item_count:2,cover_id:2}];
  window.collectionRows=[];window.collectionCalls=[];let nextCollectionId=1;
  const pathOf=c=>{const path=[];while(c){path.unshift({id:c.id,name:c.name,parent_id:c.parent_id});c=collectionRows.find(p=>p.id===c.parent_id);}return path;};
  const summary=c=>({...c,album_count:c.albums.length,person_count:c.people?.length||0,collection_count:collectionRows.filter(p=>p.parent_id===c.id).length,cover_id:c.albums[0]?.cover_id});
  const api=async(url,options)=>{
   if(url==='/api/albums')return {albums:catalog};
   if(url.startsWith('/api/albums/')){const album=catalog.find(a=>a.id===Number(url.split('/')[3]));if(options?.method==='PATCH')Object.assign(album,JSON.parse(options.body));return {...album,collection_count:collectionRows.filter(c=>c.albums.some(a=>a.id===album.id)).length};}
   const id=Number(url.split('/')[3]),c=collectionRows.find(c=>c.id===id),body=options?.body?JSON.parse(options.body):{};
   if(url.endsWith('/order')||url.endsWith('/order/root')){
    const parent=url.endsWith('/order/root')?null:id;
    const rows=body.kind==='collection'?collectionRows.filter(r=>parent===null?r.show_on_albums!==false:r.parent_id===parent):body.kind==='album'?c.albums:c.people.map(id=>({id,pilot}));
    const moved=rows.find(r=>r.id===body.id);rows.splice(rows.indexOf(moved),1);rows.splice(rows.findIndex(r=>r.id===body.target_id)+Number(body.after),0,moved);
    if(body.kind==='collection')collectionRows=[...collectionRows.filter(r=>!rows.includes(r)),...rows];
    if(body.kind==='person')c.people=rows.map(r=>r.id);
    return {order:rows.map(r=>({id:r.id,...(body.kind==='person'?{pilot}:{})}))};
   }
   if(url.includes('/people/')&&url.includes('/photos'))return {name:namedPeople[0].name,items:[{id:1,name:'Original.jpg'},{id:2,name:'Next.jpg'}]};
   if(url.endsWith('/person-options'))return {pilot,people:namedPeople,included:c.people||[]};
   if(url.endsWith('/people')){
    if(options){c.people=[...new Set([...(c.people||[]),...body.person_ids])];return {added:body.person_ids.length};}
    return {people:(c.people||[]).map(id=>({...namedPeople.find(p=>p.person_id===id),pilot,person_id:id,available:namedPeople.some(p=>p.person_id===id)}))};
   }
   if(url.endsWith('/remove-people')){c.people=c.people.filter(id=>!body.person_ids.includes(id));return {removed:body.person_ids.length};}
   if(options){collectionCalls.push({url,...options});await new Promise(r=>setTimeout(r,100));
    if(url==='/api/album-collections'){const row={id:nextCollectionId++,parent_id:null,...body,albums:[]};collectionRows.push(row);return row;}
    if(options.method==='PATCH'){Object.assign(c,body);return c;}
    if(options.method==='DELETE'){for(const row of collectionRows)if(row.parent_id===id)row.parent_id=null;collectionRows=collectionRows.filter(c=>c.id!==id);return {};}
    if(url.endsWith('/cover')){c.cover_id=body.media_id;return c;}
    if(url.endsWith('/parent')){c.parent_id=body.parent_id;if(c.parent_id===null)c.show_on_albums=true;return c;}
    if(url.endsWith('/albums')){let added=0;for(const aid of body.album_ids)if(!c.albums.some(a=>a.id===aid)){c.albums.push(catalog.find(a=>a.id===aid));added++;}return {added};}
    if(url.endsWith('/remove')){c.albums=c.albums.filter(a=>!body.album_ids.includes(a.id));return {};}
   }
   if(url.endsWith('-options'))return {collections:collectionRows.filter(row=>row.id!==id&&row.parent_id===null&&!pathOf(c).some(p=>p.id===row.id)).map(row=>({...row,path:pathOf(row).map(p=>p.name)}))};
   return url==='/api/album-collections'?{collections:collectionRows.filter(c=>c.show_on_albums!==false).map(summary)}:structuredClone({...c,person_count:c.people?.length||0,depth:pathOf(c).length,breadcrumbs:pathOf(c),collections:collectionRows.filter(row=>row.parent_id===id).map(summary)});
  };
  const openCollection=async id=>{document.querySelector('#library').hidden=true;albums.hide();if(id===null){document.querySelector('#heading').textContent='Albums';await albums.index();}else await albums.collectionDetail(id);};
  const gallery=createPeoplePhotoGallery({api,openViewer:id=>{window.openedPhoto=id;}});
  window.albums=setupAlbums({api,openCollection,openPerson:(cid,p)=>gallery(null,{url:'/api/album-collections/'+cid+'/people/'+p.person_id+'/photos?pilot='+pilot,name:p.name}),openAlbum:async(id,{collection=null}={})=>{albums.hide();document.querySelector('#library').hidden=false;if(id===null)await openCollection(null);else await albums.detail(id,collection);},refresh:async()=>{},notice:console.error});
  albums.index();
  window.collectionAction=label=>{[...document.querySelectorAll('.collection-header-actions button')].find(b=>b.textContent==='Edit collection').click();[...document.querySelectorAll('.collection-edit-actions button')].find(b=>b.textContent===label).click();};
  </script>`);return;
 }
 if(path==='/cover-fixture'){
  res.setHeader('Content-Type','text/html');res.end(`<link rel="stylesheet" href="/web/styles.css"><main><header><div><h2 id="heading"></h2><div class="library-summary-line"><p id="summary">303 items - 267.5 MB</p><span class="selection-count">2 selected</span></div></div><div class="toolbar"><input id="search" placeholder="Search filenames or source paths"></div></header><section id="library"></section></main><script type="module">
  import {setupAlbums} from '/web/albums.mjs';
  window.coverWrites=[];window.album={id:1,name:'Family',item_count:2,cover_id:1};window.albumOrder=[1,2,3];window.openedAlbums=[];
  window.albums=setupAlbums({api:async(url,options)=>{if(options){if(url.endsWith('/position')){const id=Number(url.split('/')[3]),data=JSON.parse(options.body);albumOrder=albumOrder.filter(value=>value!==id);albumOrder.splice(albumOrder.indexOf(data.target_id)+Number(data.after),0,id);return {order:albumOrder};}coverWrites.push(url);album.cover_id=2;return {};}if(url.startsWith('/api/media'))return {items:Array.from({length:60},(_,i)=>({id:i+1,name:'Photo '+i+'.jpg'})),page:1,pages:1};return url==='/api/albums'?{albums:albumOrder.map(id=>({...album,id,name:'Family '+id}))}:album;},openAlbum:id=>openedAlbums.push(id),refresh:()=>{},notice:()=>{}});
  albums.detail(1);
  </script>`);return;
 }
 if(path==='/album-fixture'){
  res.setHeader('Content-Type','text/html');res.end(`<link rel="stylesheet" href="/web/styles.css"><section id="library"></section><script type="module">
  import {setupAlbums} from '/web/albums.mjs';
  window.saved=0;window.refreshed=0;window.resolutions=0;
  window.albums=setupAlbums({api:(url,options)=>options?new Promise(resolve=>window.finishSave=resolve):Promise.resolve({albums:[{id:1,name:'Existing'}]}),openAlbum:()=>{},refresh:()=>refreshed++,notice:()=>{}});
  albums.add([1,2],{refreshAfter:false,onSaved:()=>saved++,resolveIds:()=>{resolutions++;return new Promise((resolve,reject)=>{window.resolveIds=resolve;window.rejectIds=reject});}});
  </script>`);return;
 }
 if(path==='/gallery-fixture'){
  res.setHeader('Content-Type','text/html');res.end(`<link rel="stylesheet" href="/web/styles.css"><script type="module">
  import {createPeoplePhotoGallery} from '/web/people-photo-gallery.mjs';
  window.openGallery=createPeoplePhotoGallery({api:()=>new Promise(resolve=>window.resolvePhotos=resolve),openViewer:(id,context)=>window.viewed={id,context}});
  window.openGallery(2);
  </script>`);return;
 }
 if(path.includes('/thumbnails/')||path.endsWith('/thumbnail')){res.setHeader('Content-Type','image/svg+xml');res.end('<svg xmlns="http://www.w3.org/2000/svg" width="58" height="64"><rect width="58" height="64" fill="#698e86"/><circle cx="29" cy="25" r="16" fill="#edd0aa"/></svg>');return;}
 if(path.startsWith('/api/')){res.setHeader('Content-Type','application/json');const match=path.match(/\/groups\/(\d+)$/);const pageGroups=many?[groups[0],...Array.from({length:615},(_,i)=>({...groups[1],id:100+i,section:i<405?'review':'other'}))]:groups;
  if(path.endsWith('/review-page')){
   const params=new URL(req.url,'http://localhost').searchParams,section=params.get('section');
   const selected=pageGroups.filter(g=>!g.person_id&&(g.section||(g.optional_outlier?'other':'review'))===section&&importId!=='2').map(project);
   const size=Number(params.get('page_size')||100),pages=Math.max(1,Math.ceil(selected.length/size)),page=Math.min(pages,Number(params.get('page')||1));
   res.end(JSON.stringify({groups:selected.slice((page-1)*size,page*size),total:selected.length,page,pages,page_size:size,revision:1}));return;
  }
  res.end(JSON.stringify(match?{group:groups.find(g=>g.id===Number(match[1])),revision:1,history:[{id:1,action:'correct',detection_id:20,actor:'fixture',can_undo:true}]}:{groups:pageGroups.filter(g=>importId!=='2'||g.person_id).map(project),revision:1,outlier_faces:importId?1:2}));return;}
 const file=['/','/many','/small','/large'].includes(path)?'automatic-people-preview.html':basename(path);
 try{res.setHeader('Content-Type',file.endsWith('.mjs')||file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(readFileSync(join('web',file)));}catch{res.writeHead(404).end();}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const url=`http://127.0.0.1:${server.address().port}/?desktop=1`,profile=join(out,'profile');
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,url],{windowsHide:true,stdio:'ignore'});
let ws;
try{
 for(let i=0;i<150&&!existsSync(join(profile,'DevToolsActivePort'));i++)await sleep(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 const tabs=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 ws=new WebSocket(tabs.find(t=>t.type==='page').webSocketDebuggerUrl);
 await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const wait=async expression=>{for(let i=0;i<100;i++){if(await evaluate(expression))return;await sleep(50);}throw Error('Timeout: '+expression);};
 await wait("document.querySelectorAll('.person-card').length===1");
 assert.equal(catalogRequests,0,'Main People page must not request the full review catalog');
 assert.equal(blurryRequests,0);
 assert(await evaluate("document.querySelector('#review-faces-page').hidden&&document.querySelector('.named-person-card').getBoundingClientRect().width>60"));
 await evaluate("document.querySelector('#review-faces').click()");
 await wait("!document.querySelector('#review-faces-page').hidden");
 await wait("document.querySelectorAll('.person-card').length===3");
 assert(await evaluate("['possible-matches','people-review-section','outliers'].every(id=>document.querySelector('#review-faces-page').contains(document.getElementById(id)))"));
 assert(await evaluate("document.querySelector('#possible-matches summary').contains(document.querySelector('#recheck-unnamed'))"));
 assert(await evaluate("![...document.querySelectorAll('.review-selection-tools button')].some(b=>b.textContent==='Detect again')"));
 await evaluate("document.querySelector('#outliers').parentElement.open=true");
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  const sizes=await evaluate(`Array.from(document.querySelectorAll('.unnamed-person-card')).map(c=>({width:c.getBoundingClientRect().width,height:c.getBoundingClientRect().height,text:c.textContent,label:c.querySelector('button').getAttribute('aria-label')}))`);
  assert.equal(sizes.length,2);for(const c of sizes){assert.equal(c.width,60);assert.equal(c.height,66);assert.equal(c.text,'2');assert.match(c.label,/2 photos/);}
  assert(await evaluate("document.querySelector('.named-person-card').getBoundingClientRect().width===0"));
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`cards-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 for(const section of ['review-groups','outliers']){
  assert(await evaluate(`(()=>{document.querySelector('#${section} .group-photo').click();const d=document.querySelector('#group-detail'),s=d.querySelector('.group-detail-status'),title=d.querySelector('.group-detail-title');window.previewImages=[...d.querySelectorAll('img.photo')];return s.textContent==='Updating group...'&&title.contains(s)&&d.lastElementChild.tagName==='ARTICLE'&&parseFloat(getComputedStyle(s).fontSize)<parseFloat(getComputedStyle(title).fontSize)})()`));
  await wait("document.querySelector('#group-detail')?.open&&document.querySelector('#group-detail .editable-person-name')?.textContent==='Unnamed group'&&!document.querySelector('#group-detail article[aria-busy=true]')");
  assert(await evaluate("document.querySelector('#group-detail .group-detail-status').hidden"));
  assert(await evaluate("window.previewImages.length>0&&window.previewImages.every((image,index)=>image===document.querySelectorAll('#group-detail img.photo')[index])"));
  assert(await evaluate("!document.querySelector('#group-detail').textContent.includes('Name this group')&&!Array.from(document.querySelectorAll('#group-detail button')).some(b=>b.textContent==='Save name')"));
  await evaluate("document.querySelector('#group-detail .editable-person-name').click()");
  assert(await evaluate("document.activeElement===document.querySelector('#group-detail .inline-person-name input')&&document.activeElement.value===''") );
  await evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))");
  assert(await evaluate("document.querySelector('#group-detail .inline-person-name').hidden"));
  await evaluate("document.querySelector('#group-detail .editable-person-name').click();document.querySelector('#group-detail .inline-person-name input').value='New person';document.querySelector('#group-detail .inline-person-name').requestSubmit()");
  if(section==='review-groups'){
   await wait("document.querySelector('#group-detail .is-error')?.textContent.includes('Name already exists')");
   assert(await evaluate("!document.querySelector('#group-detail .inline-person-name input').disabled"));
   await evaluate("document.querySelector('#group-detail .inline-person-name').requestSubmit()");
  }
  await wait("!!document.querySelector('#group-detail .inline-person-name')?.hidden&&!document.querySelector('#group-detail article[aria-busy=true]')");
  assert.equal(nameRequests.at(-1).path,`/api/automatic-people/groups/${section==='review-groups'?2:3}/name`);
  assert.equal(nameRequests.at(-1).name,'New person');
  assert.equal(nameRequests.at(-1).revision,1);
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`inline-name-${section}.png`),Buffer.from(shot.data,'base64'));
  await evaluate("document.querySelector('#group-detail').close()");
 }
 await evaluate("window.groupTop=document.querySelector('#review-groups').offsetTop;Array.from(document.querySelectorAll('#people-review-section button')).find(b=>b.textContent==='Select').click();document.querySelector('#review-groups .person-card').click()");
 assert(await evaluate("document.querySelector('#review-groups').offsetTop===window.groupTop"));
 assert(await evaluate("document.querySelector('#review-groups .person-card').classList.contains('group-selected')"));
 const beforeStream=catalogRequests;
 await evaluate("window.unchangedNamedCard=document.querySelector('.named-person-card');Array.from(document.querySelectorAll('#people-review-section button')).find(b=>b.textContent==='Move to person').click();document.querySelector('.person-destination-menu button').click()");
 await wait("document.querySelector('#people-review-section [role=status]').textContent.includes('1 / 2 faces')");
 assert(await evaluate("!document.querySelector('#people-review-section progress').hidden&&document.querySelector('#people-review-section progress').value===1"));
 assert(await evaluate("!/[()]|\\d+s/.test(document.querySelector('#people-review-section [role=status]').textContent)"));
 assert(await evaluate("Array.from(document.querySelectorAll('#people-review-section button')).find(b=>b.textContent==='Move to person').disabled"));
 const progressShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'bulk-review-progress.png'),Buffer.from(progressShot.data,'base64'));
 await wait("document.querySelector('#people-review-section progress').hidden");
 assert(!await evaluate("document.querySelector('#people-review-section [role=status]').textContent.includes('Updated.')"));
 assert.equal(catalogRequests,beforeStream);
 assert(await evaluate("window.unchangedNamedCard===document.querySelector('.named-person-card')"));
 assert(await evaluate("document.querySelector('#review-groups .person-card').classList.contains('group-selected')"));
 assert.equal(streamedRequests.length,1);assert.equal(streamedRequests[0].action,'match');
 assert.deepEqual(streamedRequests[0].group_faces,{'2':[20,21]});
 await cdp('Page.navigate',{url:url+'&integrated=1'});
 await wait("document.querySelectorAll('.person-card').length===1");
 await evaluate("document.querySelector('#review-faces').click()");
 await wait("!document.querySelector('#review-faces-page').hidden");
 await evaluate("document.querySelector('#auto-organize-faces').click()");
 await wait("document.querySelector('#auto-organize-faces').disabled");
 assert(await evaluate("!document.querySelector('#possible-matches summary progress').hidden"));
 await wait("!document.querySelector('#auto-organize-faces').disabled&&!document.querySelector('#undo-auto-organize').disabled");
 assert(await evaluate("document.querySelector('#possible-matches summary').textContent.includes('2 unnamed groups reduced to 1')"));
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
  const organizeShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`auto-organize-${width}.png`),Buffer.from(organizeShot.data,'base64'));
 }
 await evaluate("document.querySelector('#undo-auto-organize').click()");
 await wait("!document.querySelector('#auto-organize-faces').disabled&&document.querySelector('#undo-auto-organize').disabled");
 await wait("document.querySelector('#review-import-filter').options.length===3");
 await evaluate("document.querySelector('#people-review-section .review-selection-tools button').click();document.querySelector('#review-groups .person-card').click();const filter=document.querySelector('#review-import-filter');filter.value='1';filter.dispatchEvent(new Event('change'))");
 await wait("!document.querySelector('#review-faces-page').inert&&document.querySelector('#review-groups .unnamed-count-overlay').textContent==='1'");
 assert(await evaluate("!document.querySelector('#review-groups .group-selected')"));
 assert(await evaluate("!document.querySelector('#review-matched-people').hidden&&document.querySelectorAll('#review-matched-groups .person-card').length===1"));
 await evaluate("document.querySelector('#review-groups .group-photo').click()");
 await wait("!!document.querySelector('#group-detail article:not([aria-busy=true])')");
 assert.equal(await evaluate("document.querySelectorAll('#group-detail [data-face]').length"),1);
 await evaluate("document.querySelector('#group-detail').close();document.querySelector('#recheck-unnamed').click()");
 await wait("!document.querySelector('#recheck-unnamed').disabled");
 assert.equal(scopedCalls.at(-1).import_id,1);assert.equal(scopedCalls.at(-1).kind,'recheck');
 await evaluate("document.querySelector('#auto-organize-faces').click()");
 await wait("document.querySelector('#auto-organize-faces').disabled");
 await wait("!document.querySelector('#auto-organize-faces').disabled");
 assert.equal(scopedCalls.at(-1).import_id,1);assert.equal(scopedCalls.at(-1).kind,'organize');
 const filterShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'review-import-filter.png'),Buffer.from(filterShot.data,'base64'));
 await evaluate("filter.value='2';filter.dispatchEvent(new Event('change'))");
 await wait("!document.querySelector('#review-faces-page').inert&&document.querySelector('#people-review-section').hidden");
 assert(await evaluate("document.querySelectorAll('#review-matched-groups .person-card').length===1&&document.querySelector('#review-matched-people').textContent.includes('already matched')"));
 await evaluate("filter.value='';filter.dispatchEvent(new Event('change'))");
 await wait("!document.querySelector('#review-faces-page').inert&&document.querySelector('#review-groups .unnamed-count-overlay').textContent==='2'");
 await evaluate("window.dispatchEvent(new MessageEvent('message',{origin:location.origin,source:parent,data:{type:'familyfoto-people-action',action:'back-people'}}))");
 await wait("document.querySelector('#review-faces-page').hidden&&document.querySelector('.named-person-card').getBoundingClientRect().width>0");
 await evaluate("document.querySelector('.named-person-card .group-photo').click()");
 assert(await evaluate("!!document.querySelector('#group-detail .editable-person-name')&&!!document.querySelector('#group-detail article[aria-busy=true]')"));
 assert(await evaluate("document.querySelector('#group-detail .photo').getAttribute('src').includes('/thumbnails/')"));
 await wait("!!document.querySelector('#group-detail .editable-person-name')&&!document.querySelector('#group-detail article[aria-busy=true]')");
 const beforeCover=catalogRequests;
 await evaluate("document.querySelector('#group-detail .face-actions-trigger').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))");
 await wait("!!document.querySelector('.face-actions-panel:popover-open .choose-cover')");
 const namedMenuShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'named-face-menu.png'),Buffer.from(namedMenuShot.data,'base64'));
 assert.equal(await evaluate("document.querySelectorAll('.face-actions-panel:popover-open .move-face').length"),0);
 await evaluate("document.querySelector('.face-actions-panel:popover-open .choose-cover').click()");
 await wait("!document.querySelector('.face-actions-panel:popover-open')&&!!document.querySelector('#group-detail article:not([aria-busy=true])')");
 assert.equal(coverRequests.length,1);assert.equal(coverRequests[0].detection_id,10);assert.equal(catalogRequests,beforeCover);
 await evaluate("document.querySelector('#group-detail .face-actions-trigger').click();document.querySelector('.face-actions-panel:popover-open .face-menu-toggle').click()");
 assert.equal(await evaluate("document.querySelectorAll('.face-actions-panel:popover-open .choose-cover').length"),0);
 await evaluate("document.querySelector('.face-actions-panel:popover-open .face-actions-close').click()");
 assert.equal(await evaluate("document.querySelectorAll('#group-detail .match-group,#group-detail .rename-person').length"),0);
 assert(await evaluate("document.querySelector('#group-detail .group-review-later').disabled"));
 await evaluate("Array.from(document.querySelectorAll('#group-detail .group-album-tools button')).find(b=>b.textContent==='Select all').click()");
 assert(await evaluate("!document.querySelector('#group-detail .group-review-later').disabled"));
 await evaluate("window.addEventListener('message',e=>{if(e.data?.type==='familyfoto-add-person-album')window.albumRequest=e.data});document.querySelector('#group-detail .add-group-album').click()");
 await wait("!!window.albumRequest");
 await evaluate("window.postMessage({type:'familyfoto-person-photo-status',requestId:albumRequest.requestId,opened:false},location.origin)");
 assert(await evaluate("document.querySelector('#message').parentElement.querySelector('progress').hidden"));
 await evaluate("window.postMessage({type:'familyfoto-person-photo-status',requestId:albumRequest.requestId,opened:true},location.origin)");
 assert.equal(await evaluate("document.querySelectorAll('#group-detail .photo-selected').length"),2);
 await evaluate("window.postMessage({type:'familyfoto-person-photo-status',requestId:albumRequest.requestId,albumSaved:true},location.origin)");
 await wait("document.querySelectorAll('#group-detail .photo-selected').length===0");
 assert(await evaluate("document.querySelector('#group-detail .add-group-album').disabled&&document.querySelector('.group-selection-count').hidden"));
 await evaluate("Array.from(document.querySelectorAll('#group-detail .group-album-tools button')).find(b=>b.textContent==='Clear').click()");
 assert(await evaluate("document.querySelector('#group-detail .group-review-later').disabled"));
 assert(await evaluate("(()=>{const h=document.querySelector('.group-detail-heading');return Math.abs(h.querySelector('h2').getBoundingClientRect().top-h.querySelector('.muted').getBoundingClientRect().top)<10})()"));
 await evaluate("document.querySelector('.editable-person-name').click()");
 assert(await evaluate("!document.querySelector('.inline-person-name').hidden&&document.activeElement.name==='rename'"));
 await evaluate("document.querySelector('.inline-person-name input').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))");
 assert(await evaluate("document.querySelector('.inline-person-name').hidden"));
 await evaluate("document.querySelector('.editable-person-name').click();document.querySelector('.inline-person-name input').value='Updated name';document.querySelector('.inline-person-name').requestSubmit()");
 await wait("!!document.querySelector('.inline-person-name')?.hidden&&!document.querySelector('#group-detail article[aria-busy=true]')");
 assert.equal(renameRequests[0].name,'Updated name');assert.equal(renameRequests[0].expected_name,'Named person');
 const headerShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'group-header.png'),Buffer.from(headerShot.data,'base64'));
 await evaluate("document.querySelector('#group-detail').close()");
 await evaluate("document.querySelector('#review-faces').click()");
 await wait("!!document.querySelector('#review-groups .group-photo')");
 await evaluate("document.querySelector('#review-groups .group-photo').click()");
 await wait("!!document.querySelector('#group-detail .face-actions-trigger')&&!document.querySelector('#group-detail article[aria-busy=true]')");
 assert.equal(await evaluate("document.querySelectorAll('#group-detail input[type=checkbox]').length"),0);
 assert.equal(await evaluate("getComputedStyle(document.querySelector('#group-detail')).resize"),'both');
 assert.equal(await evaluate("document.querySelectorAll('#group-detail .view-group-photos,#group-detail summary').length"),0);
 assert.equal(await evaluate("document.querySelectorAll('#group-detail .face-original-preview').length"),0);
 assert.equal(await evaluate("document.querySelectorAll('#correction-history,.toggle-correction-history').length"),0);
 assert.equal(await evaluate("document.querySelectorAll('#group-detail .group-album-tools .undo-correction').length"),1);
 assert(await evaluate("document.querySelector('.undo-correction').disabled&&document.querySelector('.undo-correction').hidden"));
 assert(await evaluate("document.querySelector('.group-detail-heading .group-selection-count').hidden"));
 assert(!await evaluate("document.querySelector('#group-detail').textContent.includes('0 selected')"));
 assert.equal(await evaluate("getComputedStyle(document.querySelector('.face-action-status')).color"),'rgb(104, 115, 129)');
 assert(await evaluate("document.querySelector('#group-detail .bulk-move-faces').disabled&&document.querySelector('#group-detail .bulk-ignore-faces').disabled"));
 await evaluate("document.querySelector('#group-detail .face-actions-trigger').click()");
 await wait("!!document.querySelector('.face-actions-panel:popover-open')");
 assert(await evaluate("document.querySelector('.face-actions-panel:popover-open .face-context-menu').hidden"));
 assert.equal(await evaluate("document.querySelectorAll('.face-actions-panel:popover-open>.choose-cover,.face-actions-panel:popover-open>.move-face,.face-actions-panel:popover-open>.ignore-face').length"),0);
 assert(await evaluate("!!document.querySelector('.face-actions-panel:popover-open .face-actions-close svg')"));
 assert.equal(await evaluate("document.querySelectorAll('.face-actions-panel:popover-open select').length"),0);
 await evaluate("document.querySelector('.face-actions-panel:popover-open .face-menu-toggle').click()");
 assert.equal(await evaluate("document.querySelectorAll('.face-actions-panel:popover-open .choose-cover').length"),0);
 const actionSizes=await evaluate("Array.from(document.querySelectorAll('.face-actions-panel:popover-open .face-context-menu>button')).map(b=>({width:b.offsetWidth,height:b.offsetHeight}))");
 assert.equal(actionSizes.length,2);assert(actionSizes.every(s=>s.width===actionSizes[0].width&&s.height===actionSizes[0].height));
 await evaluate("document.querySelector('.face-actions-panel:popover-open .move-face').click()");
 assert.equal(await evaluate("document.querySelector('.face-actions-panel:popover-open .face-context-menu button:last-child').textContent"),'Named person');
 await evaluate("document.querySelector('.face-actions-panel:popover-open .face-context-menu button').click();document.querySelector('.face-actions-panel:popover-open .face-context-menu').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))");
 assert(await evaluate("document.querySelector('.face-actions-panel:popover-open .face-context-menu').hidden"));
 assert(await evaluate("document.querySelector('.face-original-preview img').src.endsWith('/api/media/20/thumbnail')"));
 await wait("document.querySelector('.face-original-preview img').naturalWidth>0");
 assert(await evaluate("(()=>{const r=document.querySelector('.face-actions-panel:popover-open').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight})()"));
 const panelShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'face-actions.png'),Buffer.from(panelShot.data,'base64'));
 await evaluate("window.photoMessage=null;window.addEventListener('message',e=>{if(e.data.type==='familyfoto-open-person-photo')window.photoMessage=e.data});document.querySelector('.face-original-preview').click()");
 await wait("photoMessage?.mediaId===20");
 assert.equal(await evaluate("document.querySelectorAll('.face-actions-panel:popover-open').length"),0);
 const beforeFaceAction=catalogRequests;
 await evaluate("document.querySelector('#group-detail .face-actions-trigger').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));document.querySelector('.face-actions-panel:popover-open .move-face').click();document.querySelector('.face-actions-panel:popover-open .face-context-menu button:last-child').click()");
 await wait("!document.querySelector('.face-actions-panel:popover-open')");
 assert.equal(faceActions[0].person_id,1);assert.deepEqual(faceActions[0].group_versions,{'2':'fixture-2'});
 assert(await evaluate("!document.querySelector('.undo-correction').disabled&&!document.querySelector('.undo-correction').hidden"));
 await evaluate("document.querySelector('#group-detail .face-actions-trigger').click();document.querySelector('.face-actions-panel:popover-open .face-menu-toggle').click();document.querySelector('.face-actions-panel:popover-open .ignore-face').click()");
 await wait("document.querySelector('.face-actions-panel:popover-open .face-action-status.is-error')?.textContent==='Fixture conflict; try again'");
 assert(await evaluate("!document.querySelector('.face-actions-panel:popover-open .ignore-face').disabled"));
 await evaluate("document.querySelector('.face-actions-panel:popover-open .ignore-face').click()");
 await wait("!document.querySelector('.face-actions-panel:popover-open')");
 assert.equal(faceActions.at(-1).enabled,true);assert.equal(catalogRequests,beforeFaceAction);
 assert(await evaluate("document.querySelector('.undo-correction').disabled&&document.querySelector('.undo-correction').hidden"));
 await evaluate("Array.from(document.querySelectorAll('#group-detail .group-album-tools button')).find(b=>b.textContent==='Select').click();document.querySelector('#group-detail .face-actions-trigger').click()");
 assert.equal(await evaluate("document.querySelectorAll('.face-actions-panel:popover-open').length"),0);
 assert(await evaluate("document.querySelector('#group-detail .photo').src.endsWith('/api/people-preview/thumbnails/20')"));
 assert(await evaluate("document.querySelector('#group-detail [data-face]').classList.contains('photo-selected')"));
 await evaluate("Array.from(document.querySelectorAll('#group-detail .group-album-tools button')).find(b=>b.textContent==='Clear').click()");
 const points=await evaluate("Array.from(document.querySelectorAll('#group-detail [data-face]')).map(t=>{const r=t.getBoundingClientRect();return {x:r.left+10,y:r.top+10}})");
 await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...points[0],button:'left',clickCount:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:points[1].x+30,y:points[1].y+30,button:'left',buttons:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:points[1].x+30,y:points[1].y+30,button:'left',clickCount:1});
 assert.equal(await evaluate("document.querySelectorAll('#group-detail .photo-selected').length"),2);
 assert.equal(await evaluate("document.querySelector('.group-detail-heading .group-selection-count').textContent"),'2 selected');
 assert(await evaluate("!document.querySelector('.group-selection-count').hidden"));
 await evaluate("document.querySelector('#group-detail .bulk-move-faces').click()");
 assert(await evaluate("document.querySelector('#group-detail .person-destination-menu').matches(':popover-open')"));
 assert(await evaluate("(()=>{const r=document.querySelector('.person-destination-menu').getBoundingClientRect();return r.width<=220&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()"));
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
 await wait("!document.querySelector('.person-destination-menu')");
 assert(await evaluate("document.activeElement===document.querySelector('#group-detail .bulk-move-faces')"));
 await evaluate("document.querySelector('#group-detail .bulk-move-faces').click()");
 const destinationShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'person-destination-menu.png'),Buffer.from(destinationShot.data,'base64'));
 const beforeMoveRequests=catalogRequests;
 await evaluate("window.retainedFace=document.querySelector('#group-detail img.photo');window.otherCard=document.querySelector('#outliers .person-card')");
 await evaluate("document.querySelector('#group-detail .person-destination-menu button').click()");
 await wait("document.querySelector('#group-detail')?.open&&document.querySelector('#group-detail .group-selection-count')?.hidden&&!document.querySelector('#group-detail .photos')?.inert");
 assert.equal(catalogRequests,beforeMoveRequests);
 assert(await evaluate("retainedFace===document.querySelector('#group-detail img.photo')&&otherCard===document.querySelector('#outliers .person-card')"));
 assert.equal(bulkRequests[0].action,'move');assert.deepEqual(bulkRequests[0].detection_ids,[20,21]);assert.equal(bulkRequests[0].person_id,1);
 await wait("!!document.querySelector('#group-detail .face-actions-trigger')&&!document.querySelector('#group-detail article[aria-busy=true]')");
 await evaluate("Array.from(document.querySelectorAll('#group-detail .group-album-tools button')).find(b=>b.textContent==='Select').click();document.querySelector('#group-detail .face-actions-trigger').click();document.querySelector('#group-detail .bulk-ignore-faces').click()");
 await wait("!!document.querySelector('#group-detail .bulk-ignore-faces')?.disabled&&!document.querySelector('#group-detail .photos')?.inert&&!document.querySelector('#group-detail article[aria-busy=true]')");
 assert.equal(bulkRequests[1].action,'ignore');assert.deepEqual(bulkRequests[1].detection_ids,[20]);
 await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:850,deviceScaleFactor:1,mobile:false});
 const widths=await evaluate("(()=>{const d=document.querySelector('#group-detail');d.style.width='500px';const a=d.getBoundingClientRect().width;d.style.width='1000px';return [a,d.getBoundingClientRect().width]})()");
 assert(widths[1]>widths[0]);
 assert.equal(await evaluate("getComputedStyle(document.querySelector('#group-detail .photos')).columnGap"),'6px');
 await evaluate("document.querySelector('#group-detail').style.height='500px'");
 const initial=await evaluate("(()=>{const r=document.querySelector('#group-detail').getBoundingClientRect();const b=document.querySelector('.group-detail-bar').getBoundingClientRect();return {left:r.left,top:r.top,x:b.left+100,y:b.top+12}})()");
 await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:initial.x,y:initial.y,button:'left',clickCount:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:initial.x+60,y:initial.y+40,button:'left',buttons:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:initial.x+60,y:initial.y+40,button:'left',clickCount:1});
 const moved=await evaluate("(()=>{const r=document.querySelector('#group-detail').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom}})()");
 assert(moved.left>initial.left&&moved.top>initial.top);
 assert(moved.right<=1280&&moved.bottom<=850);
 const detailShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'resizable-detail.png'),Buffer.from(detailShot.data,'base64'));
 assert(await evaluate("!!document.querySelector('#group-detail .group-review-later')"));
 assert(await evaluate("document.querySelector('#group-detail .group-review-later').disabled"));
 await evaluate("Array.from(document.querySelectorAll('#group-detail .group-album-tools button')).find(b=>b.textContent==='Select all').click()");
 const beforeLater=catalogRequests;
 await evaluate("document.querySelector('#group-detail .group-review-later').click()");
 await wait("document.querySelector('#group-detail')?.open&&!document.querySelector('#group-detail .group-review-later')");
 assert.equal(catalogRequests,beforeLater);
 assert(await evaluate("!!document.querySelector('#outliers [data-group=\"2\"]')&&!document.querySelector('#review-groups [data-group=\"2\"]')"));
 assert(await evaluate("document.querySelector('#outlier-summary').textContent.startsWith('Review later')"));
 await cdp('Page.navigate',{url:url.split('/?')[0]+'/many?desktop=1#review-faces'});
 await wait("document.querySelectorAll('#review-groups .person-card').length===100");
 assert.equal(await evaluate("document.querySelectorAll('#outliers .person-card').length"),100);
 await evaluate("document.querySelector('[aria-label=\"To be reviewed pages\"] [aria-label=\"Next page\"]').click()");
 await wait("Number(document.querySelector('#review-groups .person-card').dataset.group)===200");
 assert.equal(await evaluate("Number(document.querySelector('#review-groups .person-card').dataset.group)"),200);
 await evaluate("Array.from(document.querySelectorAll('#people-review-section .review-selection-tools button')).find(b=>b.textContent==='Select all').click()");
 assert.equal(await evaluate("document.querySelectorAll('#review-groups .group-selected').length"),100);
 await evaluate("document.querySelector('[aria-label=\"To be reviewed page\"]').value=5;document.querySelector('[aria-label=\"To be reviewed page\"]').dispatchEvent(new Event('change'))");
 await wait("document.querySelectorAll('#review-groups .person-card').length===5");
 assert.equal(await evaluate("document.querySelectorAll('#review-groups .person-card').length"),5);
 assert.equal(await evaluate("document.querySelectorAll('#review-groups .group-selected').length"),0);
 assert.equal(await evaluate("document.querySelector('[aria-label=\"Review later page\"]').value"),'1');
 await evaluate("const size=document.querySelector('[aria-label=\"To be reviewed items per page\"]');size.value='200';size.dispatchEvent(new Event('change'))");
 await wait("document.querySelectorAll('#review-groups .person-card').length===200");
 assert.equal(await evaluate("document.querySelectorAll('#review-groups .person-card').length"),200);
 assert.equal(await evaluate("document.querySelector('[aria-label=\"To be reviewed page\"]').value"),'1');
 assert.equal(await evaluate("document.querySelectorAll('#review-groups .group-selected').length"),0);
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  for(const size of ['small','large']){
   await cdp('Page.navigate',{url:url.split('/?')[0]+`/${size}?desktop=1&integrated=1`});
   await wait("!!document.querySelector('.named-person-card .group-photo')");
   await evaluate("document.querySelector('.named-person-card .group-photo').click()");
   await wait("!!document.querySelector('#group-detail article')&&!document.querySelector('#group-detail article[aria-busy=true]')");
   const rect=await evaluate("(()=>{const r=document.querySelector('#group-detail').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})()");
   assert(Math.abs(rect.x+rect.width/2-width/2)<3);assert(Math.abs(rect.y+rect.height/2-425)<3);
   assert(rect.height<=850*.96+1);
   if(size==='small'){
    assert(rect.height<500);assert.equal(await evaluate("document.querySelectorAll('#group-detail [data-face]').length"),11);
    assert.equal(await evaluate("document.querySelectorAll('.group-face-pages').length"),1);
   }else{
    assert.equal(await evaluate("document.querySelectorAll('#group-detail [data-face]').length"),100);
    await evaluate("window.allAlbumRequest=null;window.addEventListener('message',e=>{if(e.data?.type==='familyfoto-add-person-album')window.allAlbumRequest=e.data});document.querySelector('#group-detail .add-all-group-album').click()");
    await wait("!!window.allAlbumRequest");
    assert(await evaluate("allAlbumRequest.groupId===1&&allAlbumRequest.mediaIds===undefined"));
    await evaluate("window.postMessage({type:'familyfoto-person-photo-status',requestId:allAlbumRequest.requestId,opened:true},location.origin)");
    const beforePage=catalogRequests;
    await evaluate("Array.from(document.querySelectorAll('#group-detail .group-album-tools button')).find(b=>b.textContent==='Select all').click()");
    assert.equal(await evaluate("document.querySelectorAll('#group-detail .photo-selected').length"),100);
    assert(await evaluate("getComputedStyle(document.querySelector('#group-detail .photos')).overflowY==='visible'"));
    await evaluate("document.querySelector('.group-face-pages [aria-label=\"Next page\"]').click()");
    assert.equal(await evaluate("document.querySelectorAll('#group-detail .photo-selected').length"),0);
    assert.equal(await evaluate("document.querySelector('.group-face-pages input').value"),'2');
    assert.equal(await evaluate("document.querySelector('#group-detail [data-face]').dataset.face"),'1100');
    await evaluate("document.querySelector('.group-face-pages input').value=5;document.querySelector('.group-face-pages input').dispatchEvent(new Event('change'))");
    assert.equal(await evaluate("document.querySelectorAll('#group-detail [data-face]').length"),5);
    assert.equal(catalogRequests,beforePage);
    await evaluate("document.querySelector('.group-face-pages input').value=1;document.querySelector('.group-face-pages input').dispatchEvent(new Event('change'))");
    await evaluate("const size=document.querySelector('[aria-label=\"Group photos items per page\"]');size.value='50';size.dispatchEvent(new Event('change'))");
    assert.equal(await evaluate("document.querySelectorAll('#group-detail [data-face]').length"),50);
   }
   const screenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`group-${size}-${width}.png`),Buffer.from(screenshot.data,'base64'));
  }
 }
 await cdp('Page.navigate',{url:url+'&integrated=1'});
 await wait("!!document.querySelector('#blurry-faces')");
 const beforeBlurry=catalogRequests;
 await evaluate("document.querySelector('#blurry-faces').click()");
 await wait("document.querySelectorAll('#blurry-face-review figure').length===100");
 assert.equal(catalogRequests,beforeBlurry);
 assert.equal(await evaluate("getComputedStyle(document.querySelector('#groups')).display"),'none');
 assert.equal(await evaluate("document.querySelector('#blurry-face-review .restore-ignored').title"),'Send to review');
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
  await wait("[...document.querySelectorAll('#blurry-face-review img')].filter(img=>img.getBoundingClientRect().top<innerHeight).every(img=>img.complete&&img.naturalWidth>0)");
  await evaluate("document.querySelector('#blurry-face-review .face-actions-trigger').click()");
  await wait("document.querySelector('.face-actions-panel:popover-open .face-original-preview img')?.naturalWidth>0");
  assert(await evaluate("document.querySelector('.face-actions-panel:popover-open .face-original-preview img').src.endsWith('/api/media/90000/thumbnail')"));
  assert(await evaluate("document.querySelector('.face-actions-panel:popover-open .face-menu-toggle').hidden"));
  assert(await evaluate("(()=>{const r=document.querySelector('.face-actions-panel:popover-open').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight})()"));
  const screenshot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`blurry-${width}.png`),Buffer.from(screenshot.data,'base64'));
  await evaluate("document.querySelector('.face-actions-panel:popover-open .face-actions-close').click()");
 }
 await evaluate("[...document.querySelectorAll('#blurry-face-review button')].find(b=>b.textContent==='Next').click()");
 await wait("document.querySelectorAll('#blurry-face-review figure').length===1");
 await evaluate("document.querySelector('#blurry-face-review .restore-ignored').click()");
 await wait("document.querySelector('#blurry-face-review h2').textContent==='Blurry faces (100)'");
 assert.equal(blurryRestores,1);assert.equal(catalogRequests,beforeBlurry);
 await evaluate("window.postMessage({type:'familyfoto-people-action',action:'refresh'},location.origin)");
 await wait("document.querySelector('#refresh').disabled===false");
 assert.equal(catalogRequests,beforeBlurry);
 await evaluate("document.querySelector('#ignored-faces').click()");
 await wait("!!document.querySelector('#ignored-face-review .face-actions-trigger')");
 await evaluate("document.querySelector('#ignored-face-review .face-actions-trigger').click()");
 await wait("document.querySelector('.face-actions-panel:popover-open .face-original-preview img')?.naturalWidth>0");
 assert(await evaluate("document.querySelector('.face-actions-panel:popover-open .face-original-preview img').src.endsWith('/api/media/80000/thumbnail')"));
 const ignoredShot=await cdp('Page.captureScreenshot');writeFileSync(join(out,'ignored-preview.png'),Buffer.from(ignoredShot.data,'base64'));
 await cdp('Page.navigate',{url:url.split('/?')[0]+'/gallery-fixture'});
 await wait("document.querySelector('.people-photo-gallery')?.open");
 assert.equal(await evaluate("document.querySelector('[role=status]').textContent"),'Loading photos...');
 await evaluate("resolvePhotos({items:[{id:1,name:'Original.jpg',rotation:90},{id:1,name:'Original.jpg'},{id:2,name:'Second.jpg'}]})");
 await wait("document.querySelectorAll('.people-photo-gallery .tile').length===2");
 assert(await evaluate("document.querySelector('.tile img').src.endsWith('/api/media/1/thumbnail')"));
 await evaluate("document.querySelector('.tile').click()");
 assert.deepEqual(await evaluate('({id:viewed.id,count:viewed.context.items.length})'),{id:1,count:2});
 assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
 await evaluate("document.querySelector('[aria-label=\"Close group photos\"]').click()");
 assert.equal(await evaluate("document.querySelector('.people-photo-gallery').open"),false);
 await evaluate('openGallery(2);resolvePhotos({items:[]})');
 await wait("document.querySelector('[role=status]').textContent==='No available photos in this group'");
 await cdp('Page.navigate',{url:url.split('/?')[0]+'/album-fixture'});
 await wait("document.querySelector('#albumTarget')?.disabled===false");
 assert(await evaluate("document.querySelector('#albumDialog progress').hidden"));
 assert.equal(await evaluate('resolutions'),0);
 await evaluate("document.querySelector('#albumTarget').value='1';document.querySelector('#albumTarget').dispatchEvent(new Event('change'));document.querySelector('#albumForm').requestSubmit()");
 await wait("!!window.resolveIds");
 assert(await evaluate("!window.finishSave&&!document.querySelector('#albumDialog progress').hidden&&resolutions===1"));
 await evaluate("rejectIds(Error('Photo validation failed'))");
 await wait("document.querySelector('#albumError').textContent==='Photo validation failed'");
 assert(await evaluate("document.querySelector('#albumDialog').open&&!document.querySelector('#albumSave').disabled&&saved===0"));
 await evaluate("document.querySelector('#albumForm').requestSubmit();resolveIds([1,2])");
 await wait("!!window.finishSave");
 assert(await evaluate("!document.querySelector('#albumDialog progress').hidden&&document.querySelector('#albumSave').disabled&&saved===0"));
 await evaluate("finishSave({added:2})");
 await wait("!document.querySelector('#albumDialog').open");
 assert.deepEqual(await evaluate('({saved,refreshed})'),{saved:1,refreshed:0});
 await cdp('Page.navigate',{url:url.split('/?')[0]+'/?desktop=1&integrated=1&import=2#review-faces'});
 await wait("document.querySelector('#review-import-filter')?.value==='2'&&document.querySelector('#people-review-section').hidden&&!document.querySelector('#review-matched-people').hidden");
 assert(await evaluate("document.querySelector('#review-matched-people').textContent.includes('already matched')"));
 assert.equal(await evaluate("(()=>{window.dispatchEvent(new MessageEvent('message',{origin:location.origin,source:window,data:{type:'familyfoto-people-action',action:'back-people'}}));return document.querySelector('#groups').children.length})()"),0);
 await wait("document.querySelector('#review-faces-page').hidden&&document.querySelector('#groups').children.length>0");
 assert(await evaluate("!new URLSearchParams(location.search).has('import')&&document.querySelector('#review-import-filter').value===''"));
 await cdp('Page.navigate',{url:url.split('/?')[0]+'/cover-fixture'});
 await wait("Array.from(document.querySelectorAll('#albumHeader button')).some(b=>b.textContent==='Edit album')");
 assert(!await evaluate("Array.from(document.querySelectorAll('#albumHeader button')).some(b=>b.textContent==='Change cover')"));
 assert(await evaluate("document.querySelector('main > header').hidden&&document.querySelector('#albumHeader #heading')&&document.querySelector('#albumHeader .library-summary-line .selection-count')&&document.querySelector('#albumHeader #search')"));
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth&&document.querySelector('#search').getBoundingClientRect().width>150"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`album-header-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await evaluate("void albums.action('cover',[2])");
 await wait("!!document.querySelector('.confirm-dialog[open]')");
 await evaluate("document.querySelector('.confirm-dialog .btn-primary').click()");
 await wait("!document.querySelector('#albumDialog').open&&document.querySelector('#albumHeader .album-cover img')?.src.endsWith('/2/thumbnail')");
 assert.deepEqual(await evaluate('coverWrites'),['/api/albums/1/items/2/cover']);
 await evaluate("albums.action('transfer',[1,2])");
 await wait("document.querySelector('#albumTarget')?.disabled===false");
 assert(await evaluate("document.querySelector('#albumForm h2').textContent==='Move to album'&&document.querySelector('#albumSave').textContent==='Move'&&![...document.querySelector('#albumTarget').options].some(o=>o.value==='1')&&document.querySelector('#albumDialog progress').hidden"));
 await evaluate("document.querySelector('#albumTarget').value='2';document.querySelector('#albumTarget').dispatchEvent(new Event('change'));document.querySelector('#albumForm').requestSubmit()");
 await wait("!document.querySelector('#albumDialog').open");
 assert.equal((await evaluate('coverWrites')).at(-1),'/api/albums/1/transfer');
 await evaluate("albums.add([1])");await wait("document.querySelector('#albumTarget')?.disabled===false");
 assert(await evaluate("document.querySelector('#albumTarget').value==='2'&&document.querySelector('#albumName').parentElement.hidden&&!document.querySelector('#albumName').required"));
 await evaluate("document.querySelector('#albumTarget').value='3';document.querySelector('#albumTarget').dispatchEvent(new Event('change'));document.querySelector('#albumDialog').close();albums.add([1])");
 await wait("document.querySelector('#albumTarget')?.disabled===false");
 assert.equal(await evaluate("document.querySelector('#albumTarget').value"),'2');
 await evaluate("document.querySelector('#albumDialog').close()");
 await evaluate("album.description='A long album description that must never change the size of its card or wrap onto another line';albums.index()");await wait("!!document.querySelector('.album-card')");
 assert(await evaluate("!document.querySelector('main > header').hidden&&document.querySelector('main > header #heading')&&document.querySelector('main > header #search')&&document.querySelector('main > header .selection-count')"));
 await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:850,deviceScaleFactor:1,mobile:false});
 const albumPoints=await evaluate("[...document.querySelectorAll('.album-card')].map(c=>{const r=c.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+30}})");
 await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...albumPoints[0],button:'left',clickCount:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:albumPoints[2].x+20,y:albumPoints[2].y,button:'left',buttons:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:albumPoints[2].x+20,y:albumPoints[2].y,button:'left',clickCount:1});
 await wait("[...document.querySelectorAll('.album-card')].map(c=>c.dataset.album).join(',')==='2,3,1'");
 assert.deepEqual(await evaluate('openedAlbums'),[]);
 await evaluate('albums.index()');await wait("[...document.querySelectorAll('.album-card')].map(c=>c.dataset.album).join(',')==='2,3,1'");
 assert(await evaluate("document.querySelector('.album-cover .album-count-overlay').textContent==='2'&&!document.querySelector('.album-card .album-count')&&!document.querySelector('.album-card-copy').textContent.includes('items')"));
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate("document.querySelector('.album-card .album-cover').getBoundingClientRect().width<180&&document.documentElement.scrollWidth<=innerWidth"));
  assert(await evaluate("[...document.querySelectorAll('.album-card')].every(c=>{const r=c.getBoundingClientRect(),d=c.querySelector('.album-card-description');return r.width===128&&r.height===142&&d.title===d.textContent&&getComputedStyle(d).textOverflow==='ellipsis'&&getComputedStyle(d).whiteSpace==='nowrap'})"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`albums-small-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 assert.match(readFileSync('web/index.html','utf8'),/<nav[^>]*><button data-view="albums">/);
 await cdp('Page.navigate',{url:url.split('/?')[0]+'/collection-fixture'});
 await wait("document.querySelectorAll('.album-card').length===2");
 await evaluate("[...document.querySelectorAll('.albums-header-actions button')].find(b=>b.textContent==='Create collection').click()");
 await evaluate("document.querySelector('#albumDialog input').value='Family trips';document.querySelector('#albumDialog textarea').value='Albums from our holidays';document.querySelector('#albumDialog form').requestSubmit()");
 await wait("document.querySelector('#heading').textContent==='Family trips'&&!document.querySelector('#albumDialog').open");
 assert(await evaluate("document.querySelector('#albums').textContent.includes('No albums or collections here yet.')"));
 await evaluate("collectionAction('Add albums')");
 await wait("document.querySelectorAll('.collection-picker .album-card').length===2");
 await evaluate("document.querySelectorAll('.collection-picker .album-card').forEach(b=>b.click());[...document.querySelectorAll('#albumDialog button')].find(b=>b.textContent==='Add').click()");
 await wait("!document.querySelector('#albumDialog').open&&document.querySelectorAll('#albums .album-card').length===2");
 assert(await evaluate("document.querySelector('main > header').hidden&&document.querySelector('.collection-detail-summary h2').textContent==='Family trips'"));
 assert.deepEqual(await evaluate("[...document.querySelectorAll('.collection-header-actions button')].map(b=>b.textContent)"),['Edit collection','Back to Albums']);
 assert(!await evaluate("[...document.querySelectorAll('#albums button')].some(b=>b.textContent==='Refresh people')"));
 await evaluate("document.querySelector('[aria-label=\"Edit collection name\"]').click();document.querySelector('.collection-detail-summary h2 input').value='Renamed trips';document.querySelector('.collection-detail-summary h2 [aria-label=\"Save\"]').click()");
 await wait("document.querySelector('.collection-detail-summary h2').textContent==='Renamed trips'");
 assert.equal(await evaluate("document.querySelector('.collection-breadcrumbs [aria-current]').textContent"),'Renamed trips');
 await evaluate("document.querySelector('[aria-label=\"Edit collection name\"]').click();document.querySelector('.collection-detail-summary h2 input').value='Family trips';document.querySelector('.collection-detail-summary h2 [aria-label=\"Save\"]').click()");
 await wait("document.querySelector('.collection-detail-summary h2').textContent==='Family trips'");
 await evaluate("document.querySelector('[aria-label=\"Edit collection description\"]').click();document.querySelector('.album-description input').value='Discard me';document.querySelector('.album-description [aria-label=\"Cancel\"]').click()");
 assert.equal(await evaluate("document.querySelector('.album-description').textContent"),'Albums from our holidays');
 await evaluate("collectionAction('Choose cover')");
 await wait("document.querySelectorAll('.collection-cover-choices .album-card').length===2");
 await evaluate("document.querySelectorAll('.collection-cover-choices .album-card')[1].click()");
 await wait("!document.querySelector('#albumDialog').open&&collectionRows[0].cover_id===2");
 await evaluate("collectionAction('Select')");
 assert(await evaluate("[...document.querySelectorAll('.collection-tools button')].find(b=>b.textContent==='Use as cover').disabled"));
 await evaluate("document.querySelector('#albums .album-card').click()");
 assert(!await evaluate("[...document.querySelectorAll('.collection-tools button')].find(b=>b.textContent==='Use as cover').disabled"));
 await evaluate("[...document.querySelectorAll('.collection-tools button')].find(b=>b.textContent==='Use as cover').click()");
 await wait("collectionRows[0].cover_id===1&&[...document.querySelectorAll('.collection-tools button')].some(b=>b.textContent==='Select')");
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`collection-header-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await evaluate("document.querySelector('#albums .album-card').click()");
 await wait("[...document.querySelectorAll('#albumHeader button')].some(b=>b.textContent==='Back to collection')");
 await evaluate("[...document.querySelectorAll('#albumHeader button')].find(b=>b.textContent==='Back to collection').click()");
 await wait("!document.querySelector('#albums').hidden&&document.querySelector('#heading').textContent==='Family trips'");
 await evaluate("collectionAction('Select');document.querySelector('#albums .album-card').click();[...document.querySelectorAll('#albums button')].find(b=>b.textContent==='Remove albums').click()");
 await wait("document.querySelectorAll('#albums .album-card').length===1");
 assert(!await evaluate("!!document.querySelector('.confirm-dialog[open]')"));
 await evaluate("[...document.querySelectorAll('#albums button')].find(b=>b.textContent==='Edit collection').click();document.querySelector('#albumDialog input').value='Travel';document.querySelector('#albumDialog form').requestSubmit()");
 await wait("document.querySelector('#heading').textContent==='Travel'&&!document.querySelector('#albumDialog').open");
 await evaluate("[...document.querySelectorAll('.collection-header-actions button')].find(b=>b.textContent==='Back to Albums').click()");
 await wait("document.querySelectorAll('.album-card').length===3");
 await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:850,deviceScaleFactor:1,mobile:false});
 const dropPoints=await evaluate("[document.querySelector('.album-card[data-album=\"1\"]'),document.querySelector('.album-collection-card')].map(c=>{const r=c.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})");
 for(let attempt=0;attempt<2;attempt++){
  await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...dropPoints[0],button:'left',clickCount:1});
  await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',...dropPoints[1],button:'left',buttons:1});
  await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',...dropPoints[1],button:'left',clickCount:1});
  await wait("document.querySelector('.album-collection-card .album-count-overlay').textContent==='2'&&!document.querySelector('[aria-busy=\"true\"]')");
 }
 assert(await evaluate("document.querySelectorAll('.album-card').length===3&&collectionRows[0].albums.length===2"));
 await evaluate("document.querySelector('.album-card[data-album=\"1\"]').click()");
 await wait("!!document.querySelector('#albumHeader .album-detail-summary')");
 await evaluate("[...document.querySelectorAll('#albumHeader button')].find(b=>b.textContent==='Edit album').click()");
 assert(await evaluate("!document.querySelector('#albumVisibility').disabled"));
 await evaluate("document.querySelector('#albumVisibility').checked=false;document.querySelector('#albumForm').requestSubmit()");
 await wait("!document.querySelector('#albumDialog').open");
 await evaluate("[...document.querySelectorAll('#albumHeader button')].find(b=>b.textContent==='All albums').click()");
 await wait("document.querySelectorAll('#albums .album-card').length===2");
 assert(!await evaluate("!!document.querySelector('#albums .album-card[data-album=\"1\"]')"));
 await evaluate("document.querySelector('.album-collection-card').click()");
 await wait("document.querySelectorAll('#albums .album-card').length===2");
 await evaluate("document.querySelector('#albums .album-card[data-album=\"1\"]').click()");
 await wait("!!document.querySelector('#albumHeader .album-detail-summary')");
 await evaluate("[...document.querySelectorAll('#albumHeader button')].find(b=>b.textContent==='Edit album').click();document.querySelector('#albumVisibility').checked=true;document.querySelector('#albumForm').requestSubmit()");
 await wait("!document.querySelector('#albumDialog').open");
 await evaluate("[...document.querySelectorAll('#albumHeader button')].find(b=>b.textContent==='All albums').click()");
 await wait("document.querySelectorAll('#albums .album-card').length===3");
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth&&[...document.querySelectorAll('.album-card')].every(c=>c.getBoundingClientRect().height===142)"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`collections-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await evaluate("document.querySelector('.album-collection-card').click()");
 await wait("!!document.querySelector('.collection-tools')");
 await evaluate("collectionAction('Delete collection')");
 await wait("!!document.querySelector('.confirm-dialog[open]')");
 await evaluate("[...document.querySelectorAll('.confirm-dialog button')].find(b=>b.textContent==='Delete collection').click()");
 await wait("document.querySelectorAll('.album-card').length===2&&!document.querySelector('.album-collection-card')&&[...document.querySelectorAll('.albums-header-actions button')].some(b=>b.textContent==='Create collection')");
 await evaluate("[...document.querySelectorAll('.albums-header-actions button')].find(b=>b.textContent==='Create collection').click();document.querySelector('#albumDialog input').value='C1';document.querySelector('#albumDialog form').requestSubmit()");
 await wait("document.querySelector('#heading').textContent==='C1'&&!document.querySelector('#albumDialog').open");
 for(const name of ['C2','C3','C4']){
  await evaluate("collectionAction('Add collection')");
  await wait("document.querySelector('#albumDialog select')?.options.length>0");
  await evaluate("[...document.querySelectorAll('#albumDialog button')].find(b=>b.textContent==='Add').click()");
  await wait("!!document.querySelector('#albumDialog form')");
  await evaluate(`document.querySelector('#albumDialog input').value=${JSON.stringify(name)};document.querySelector('#albumDialog form').requestSubmit()`);
  await wait(`document.querySelector('#heading').textContent===${JSON.stringify(name)}&&!document.querySelector('#albumDialog').open`);
 }
 await evaluate("[...document.querySelectorAll('.collection-header-actions button')].find(b=>b.textContent==='Edit collection').click()");
 assert(!await evaluate("[...document.querySelectorAll('.collection-edit-actions button')].some(b=>b.textContent==='Add collection')"));
 await evaluate("document.querySelector('#albumDialog').close()");
 assert(await evaluate("document.querySelector('.collection-breadcrumbs').textContent.includes('C1/C2/C3/C4')"));
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`nested-collection-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await evaluate("[...document.querySelectorAll('.collection-breadcrumbs button')].find(b=>b.textContent==='C3').click()");
 await wait("document.querySelector('#heading').textContent==='C3'");
 await evaluate("document.querySelector('.collection-detach').click()");
 await wait("!document.querySelector('.collection-child')");
 await evaluate("[...document.querySelectorAll('.collection-header-actions button')].find(b=>b.textContent==='Back to Albums').click()");
 await wait("document.querySelectorAll('.album-collection-card').length===4");
 assert(await evaluate("[...document.querySelectorAll('.album-collection-card strong')].map(n=>n.textContent).join(',')==='C1,C2,C3,C4'"));
 await evaluate("[...document.querySelectorAll('.album-collection-card')].find(c=>c.querySelector('strong').textContent==='C1').click()");
 await wait("document.querySelector('#heading').textContent==='C1'");
 await evaluate("collectionAction('Add collection')");
 await wait("[...document.querySelector('#albumDialog select').options].some(o=>o.textContent==='C4')");
 await evaluate("const s=document.querySelector('#albumDialog select');s.value=[...s.options].find(o=>o.textContent==='C4').value;[...document.querySelectorAll('#albumDialog button')].find(b=>b.textContent==='Add').click()");
 await wait("!document.querySelector('#albumDialog').open&&document.querySelectorAll('.collection-child').length===2");
 await evaluate("[...document.querySelectorAll('.album-collection-card')].find(c=>c.querySelector('strong').textContent==='C4').click()");
 await wait("document.querySelector('#heading').textContent==='C4'");
 await evaluate("collectionAction('Move collection')");
 await wait("document.querySelector('#albumDialog select')?.options.length>0");
 await evaluate("document.querySelector('#albumDialog select').value='';[...document.querySelectorAll('#albumDialog button')].find(b=>b.textContent==='Move').click()");
 await wait("!document.querySelector('#albumDialog').open&&document.querySelectorAll('.collection-breadcrumbs button').length===2");
 await evaluate("collectionAction('Add people')");
 await wait("!!document.querySelector('#albumDialog .collection-person-card')");
 await evaluate("document.querySelector('#albumDialog .collection-person-card').click();[...document.querySelectorAll('#albumDialog button')].find(b=>b.textContent==='Add').click()");
 await wait("!document.querySelector('#albumDialog').open&&!!document.querySelector('#albums .collection-person-card')");
 assert(await evaluate("document.querySelector('#albums .collection-person-card strong').textContent==='Live person'"));
 assert(await evaluate("document.querySelector('.collection-people .collection-detach').title==='Remove from collection'&&!!document.querySelector('.collection-people .collection-detach svg.lucide-x')"));
 await evaluate("document.querySelector('#albums .collection-person-card').click()");
 await wait("document.querySelectorAll('.people-photo-gallery .tile').length===2");
 await evaluate("document.querySelector('.people-photo-gallery .tile').click()");
 assert.equal(await evaluate('window.openedPhoto'),1);
 await evaluate("document.querySelector('.people-photo-gallery').close();namedPeople[0].name='Renamed live';namedPeople[0].photo_count=3;albums.collectionDetail(Number(document.querySelector('.collection-breadcrumbs [aria-current]').dataset.id))");
 await wait("document.querySelector('#albums .collection-person-card strong').textContent==='Renamed live'");
 assert(await evaluate("document.querySelector('#albums .collection-person-card .album-count-overlay').textContent==='3'"));
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:850,deviceScaleFactor:1,mobile:false});
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`collection-people-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await evaluate("collectionAction('Add people')");
 await wait("!!document.querySelector('#albumDialog .collection-person-card')");
 assert(await evaluate("document.querySelector('#albumDialog .collection-person-card').disabled"));
 await evaluate("document.querySelector('#albumDialog').close();namedPeople=[];albums.collectionDetail(Number(document.querySelector('.collection-breadcrumbs [aria-current]').dataset.id))");
 await wait("document.querySelector('#albums .collection-person-card').disabled");
 await evaluate("document.querySelector('.collection-people .collection-detach').click()");
 await wait("!document.querySelector('#albums .collection-person-card')");
 assert(await evaluate("collectionRows.every(c=>!c.people?.length)"));
 await evaluate("namedPeople=[{person_id:7,name:'First person',photo_count:2},{person_id:8,name:'Second person',photo_count:1}];collectionRows=[{id:101,name:'First collection',parent_id:null,albums:[{id:1,name:'First album',item_count:2},{id:2,name:'Second album',item_count:1}],people:[7,8]},{id:102,name:'Second collection',parent_id:null,albums:[]},{id:201,name:'First child',parent_id:101,albums:[]},{id:202,name:'Second child',parent_id:101,albums:[]}];void albums.index()");
 await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:1000,deviceScaleFactor:1,mobile:false});
 await wait("document.querySelectorAll('#albums .album-collection-card').length===4");
 async function dragCollectionCards(selector){
  const points=await evaluate(`[...document.querySelectorAll(${JSON.stringify(selector)})].slice(0,2).map(c=>{c.scrollIntoView({block:'nearest'});const r=c.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})`);
  assert.equal(points.length,2);
  await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...points[0],button:'left',clickCount:1});
  await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:points[1].x+20,y:points[1].y,button:'left',buttons:1});
  await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:points[1].x+20,y:points[1].y,button:'left',clickCount:1});
  await wait("!document.querySelector('#albums [aria-busy=\"true\"]')");
  await sleep(50);
 }
 await dragCollectionCards('#albums .album-collection-card');
 assert(await evaluate("[...document.querySelectorAll('#albums .album-collection-card')].map(c=>Number(c.dataset.collection)).join(',')==='102,101,201,202'"));
 await evaluate("document.querySelector('[data-collection=\"101\"]').click()");
 await wait("document.querySelectorAll('.collection-people .album-card').length===2");
 await dragCollectionCards('#albums .album-collection-card');
 assert(await evaluate("[...document.querySelectorAll('#albums .album-collection-card')].map(c=>Number(c.dataset.collection)).join(',')==='202,201'"));
 await dragCollectionCards('#albums .album-card[data-album]');
 assert(await evaluate("[...document.querySelectorAll('#albums .album-card[data-album]')].map(c=>Number(c.dataset.album)).join(',')==='2,1'"));
 await dragCollectionCards('.collection-people .album-card');
 assert(await evaluate("[...document.querySelectorAll('.collection-people .album-card')].map(c=>Number(c.dataset.person)).join(',')==='8,7'"));
 assert(await evaluate("[...document.querySelectorAll('#albums .collection-detach')].every(b=>b.title==='Remove from collection'&&!!b.querySelector('svg.lucide-x'))"));
 await evaluate("void albums.collectionDetail(101)");
 await wait("document.querySelectorAll('.collection-people .album-card').length===2");
 assert(await evaluate("[...document.querySelectorAll('#albums .album-collection-card')].map(c=>Number(c.dataset.collection)).join(',')==='202,201'&&[...document.querySelectorAll('#albums .album-card[data-album]')].map(c=>Number(c.dataset.album)).join(',')==='2,1'&&[...document.querySelectorAll('.collection-people .album-card')].map(c=>Number(c.dataset.person)).join(',')==='8,7'"));
 for(const width of [1280,390]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`collection-order-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await evaluate("document.querySelector('#albums .album-card[data-album]').closest('.collection-child').querySelector('.collection-detach').click()");
 await wait("document.querySelectorAll('#albums .album-card[data-album]').length===1");
 assert(!await evaluate("!!document.querySelector('.confirm-dialog[open]')"));
 await evaluate("document.querySelector('[data-collection=\"201\"]').click()");
 await wait("document.querySelector('#heading').textContent==='First child'");
 await evaluate("[...document.querySelectorAll('#albums button')].find(b=>b.textContent==='Edit collection').click()");
 assert(await evaluate("document.querySelector('#collectionVisibility').checked&&!document.querySelector('#collectionVisibility').disabled"));
 await evaluate("document.querySelector('#collectionVisibility').checked=false;document.querySelector('#albumForm').requestSubmit()");
 await wait("!document.querySelector('#albumDialog').open");
 await evaluate("[...document.querySelectorAll('.collection-header-actions button')].find(b=>b.textContent==='Back to Albums').click()");
 await wait("document.querySelectorAll('#albums .album-collection-card').length===3");
 assert(!await evaluate("!!document.querySelector('[data-collection=\"201\"]')"));
 await evaluate("document.querySelector('[data-collection=\"101\"]').click()");
 await wait("!!document.querySelector('[data-collection=\"201\"]')");
 await evaluate("document.querySelector('[data-collection=\"201\"]').click()");
 await wait("document.querySelector('#heading').textContent==='First child'");
 await evaluate("[...document.querySelectorAll('#albums button')].find(b=>b.textContent==='Edit collection').click();document.querySelector('#collectionVisibility').checked=true;document.querySelector('#albumForm').requestSubmit()");
 await wait("!document.querySelector('#albumDialog').open");
 await evaluate("[...document.querySelectorAll('.collection-header-actions button')].find(b=>b.textContent==='Back to Albums').click()");
 await wait("document.querySelectorAll('#albums .album-collection-card').length===4");
 console.log(JSON.stringify({ok:true,checks:['desktop/mobile compact cards','preview menus and keyboard controls','cover/move/ignore and error retry','ignored/blurry original previews','select group','album progress and success callback','all-pages album action','Albums first in navigation'],artifacts:out}));
 await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
