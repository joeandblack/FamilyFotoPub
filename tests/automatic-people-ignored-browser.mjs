import {chromePath} from './chrome.mjs';
// Synthetic isolated preview; no production API or personal images.
import {createServer} from 'node:http';
import {readFileSync,writeFileSync,mkdtempSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const out=mkdtempSync(join(resolve('temp'),'ignored-people-')),sleep=ms=>new Promise(r=>setTimeout(r,ms));
let restored=false,posts=0;
let orderPosts=0;
let suggestionDecision=null;
let suggestionDone=false;
const groups=[{id:1,person_id:1,name:'Meilin',section:'people',faces:[{detection_id:1,media_id:1},{detection_id:2,media_id:2}],cover_detection_id:1},{id:2,person_id:null,name:null,section:'other',optional_outlier:true,faces:[{detection_id:3,media_id:3}]}];
const server=createServer(async(req,res)=>{
 req.url=req.url.replace('/api/people-preview','/api/automatic-people');
 if(req.url.endsWith('/thumbnail')||req.url.includes('/thumbnails/')){res.setHeader('Content-Type','image/png');res.end(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64'));return;}
 if(req.url.startsWith('/api/automatic-people')){
  let data={groups,outlier_faces:1,revision:restored?2:1};
  if(req.url.endsWith('/groups/1'))data={group:groups[0],history:[],revision:1};
  if(req.url.endsWith('/groups/2'))data={group:groups[1],history:[],revision:1};
  if(req.url.endsWith('/groups/6'))data={group:groups.find(group=>group.id===6),history:[],revision:1};
  if(req.method==='POST'){
   if(req.url.endsWith('/suggestions/decision')){
    let body='';for await(const chunk of req)body+=chunk;
    suggestionDecision=JSON.parse(body);
    if(suggestionDecision.decision==='confirm'){
     assert.deepEqual(suggestionDecision,{revision:2,run_id:10,detection_id:5,person_id:1,decision:'confirm'});suggestionDone=true;
    }else assert.deepEqual(suggestionDecision,{revision:1,run_id:10,detection_id:3,person_id:1,decision:'reject'});
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ok:true}));return;
   }
   if(req.url.endsWith('/position')){
    let body='';for await(const chunk of req)body+=chunk;
    const payload=JSON.parse(body);assert.equal(payload.target_id,4);assert.equal(payload.after,true);orderPosts++;
    const first=groups.shift();groups.push(first);
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify({moved:true}));return;
   }
   assert.equal(req.url,'/api/automatic-people/faces/7/ignored');
   let body='';for await(const chunk of req)body+=chunk;
   assert.deepEqual(JSON.parse(body),{revision:1,enabled:false});restored=true;posts++;data={ok:true};
  }else if(req.url.includes('/ignored?'))data={items:restored?[]:[{detection_id:7,media_id:3,name:'Synthetic photo.jpg'}],total:restored?0:1,page:1,pages:1,revision:1};
  else if(req.url.includes('/suggestions?')){
   const items=[{detection_id:3,person_id:1,anchor_id:1,name:'Meilin',score:.35,group_id:2},{detection_id:5,person_id:1,anchor_id:1,name:'Meilin',score:.32,group_id:2}].filter(item=>!suggestionDone&&(!suggestionDecision||item.detection_id!==3));
   data={items,total:items.length,page:1,pages:1,run_id:10,revision:suggestionDecision?2:1};
  }
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;
 }
 const path=new URL(req.url,'http://fixture').pathname;
 const scripts=['automatic-people-preview.mjs','people-sections.mjs','people-review-selection.mjs','people-order.mjs','people-suggestions.mjs','selection.mjs','lucide.min.js'];
 const script=scripts.find(name=>path==='/web/'+name);
 res.setHeader('Content-Type',script?'text/javascript':'text/html');
 res.end(readFileSync(join('web',script||'automatic-people-preview.html')));
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
groups.push({id:4,person_id:4,name:'Mei',section:'people',faces:[{detection_id:4,media_id:4}],cover_detection_id:4});
const url=`http://127.0.0.1:${server.address().port}/?desktop=1&integrated=1`,profile=join(out,'profile');
const chrome=spawn(chromePath(),['--headless','--no-first-run','--disable-background-networking','--remote-debugging-port=0',`--user-data-dir=${profile}`,url],{windowsHide:true,stdio:'ignore'});let ws;
try{
 for(let n=0;n<150&&!existsSync(join(profile,'DevToolsActivePort'));n++)await sleep(100);
 const port=readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];
 const tabs=await(await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 ws=new WebSocket(tabs.find(t=>t.url===url).webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0;const pending=new Map();
 ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}};
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const key=++id;pending.set(key,{resolve,reject});ws.send(JSON.stringify({id:key,method,params}));});
 const run=async expression=>{const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const wait=async exp=>{for(let n=0;n<100;n++){if(await run(exp))return;await sleep(50);}throw Error(exp);};
 await wait("!!document.querySelector('#ignored-faces')");
 await wait("!!document.querySelector('.named-person-card .group-photo')");
 assert.equal(await run("document.querySelector('.named-person-card .person-name-overlay').textContent"),'Meilin2');
 assert.equal(await run("document.querySelectorAll('.named-person-card .rename-person,.named-person-card .move-person').length"),0);
 assert.equal(await run("document.querySelectorAll('.person-card .match-group,.person-card .card-actions,.person-card .add-group-album,.person-card .group-detail').length"),0);
 assert.equal(await run("document.querySelectorAll('#outliers .move-person').length"),0);
 assert.equal(await run("!!document.querySelector('#outliers input[name=name]')"),false);
 assert.equal(await run("getComputedStyle(document.querySelector('.person-name-overlay')).backgroundColor"),'rgba(0, 0, 0, 0)');
 await run("document.querySelector('#outliers .group-photo').click()");
 await wait("!!document.querySelector('#group-detail input[name=name]')");
 await run("document.querySelector('#group-detail').close()");
 await run("document.querySelector('.named-person-card .group-photo').click()");
 await wait("!!document.querySelector('#group-detail .group-album-tools')");
 assert.equal(await run("document.querySelector('#group-detail .add-group-album').disabled"),true);
 await run("document.querySelector('#group-detail .group-album-tools button').click()");
 assert.equal(await run("document.querySelector('#group-detail .group-album-tools span').textContent"),'2 selected');
 assert.equal(await run("document.querySelector('#group-detail .add-group-album').disabled"),false);
 assert.equal(await run("document.querySelectorAll('#group-detail .choose-cover').length"),2);
 await run("document.querySelector('#group-detail').close();document.querySelector('#outliers').parentElement.open=true");
 const positions=await run("[1,4].map(id=>{const r=document.querySelector('#groups [data-group=\"'+id+'\"] .group-photo').getBoundingClientRect();return {x:r.left+r.width*.8,y:r.top+r.height/2}})");
 await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...positions[0],button:'left',buttons:1,clickCount:1});
 for(let n=1;n<=8;n++)await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:positions[0].x+(positions[1].x-positions[0].x)*n/8,y:positions[0].y,button:'left',buttons:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',...positions[1],button:'left',buttons:0,clickCount:1});
 await wait("document.querySelector('#groups .named-person-card').dataset.group==='4'");
 assert.equal(orderPosts,1);assert.equal(await run("document.querySelector('#group-detail').open"),false);
 await wait("document.querySelector('#possible-matches summary>span').textContent==='Possible matches (2)'");
 assert.equal(suggestionDecision,null);
 await run("document.querySelector('#possible-matches').open=true");
 await wait("document.querySelectorAll('.suggestion-pair img').length===4");
 assert.equal(await run("document.querySelector('#ignored-face-review').hidden"),true);
 assert.equal(await run("document.querySelector('main').lastElementChild.id"),'ignored-face-review');
 assert.equal(await run("document.querySelector('#ignored-face-review img')===null"),true);
 await run("document.querySelector('#ignored-faces').click()");
 await wait("!document.querySelector('#ignored-face-review')?.hidden&&!!document.querySelector('.restore-ignored')");
 assert.equal(await run("getComputedStyle(document.querySelector('#groups')).display"),'none');
 assert.equal(await run("!!document.querySelector('.restore-ignored svg')"),true);
 assert.equal(await run("document.querySelector('.restore-ignored').title"),'Restore face');
 assert.equal(await run("document.querySelector('.ignored-face-grid figcaption').title"),'Synthetic photo.jpg');
 assert.equal(await run("getComputedStyle(document.querySelector('.ignored-face-grid figcaption')).whiteSpace"),'nowrap');
 assert.equal(await run("getComputedStyle(document.querySelector('.ignored-face-grid figcaption')).textOverflow"),'ellipsis');
 assert.equal(await run("getComputedStyle(document.querySelector('.ignored-face-grid figcaption')).fontSize"),'6px');
 for(const [width,height] of [[1280,900],[390,844]]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
  assert(await run("(()=>{const d=document.querySelector('#ignored-face-review');return d.scrollWidth<=d.clientWidth+1})()"));
  const shot=await cdp('Page.captureScreenshot');writeFileSync(join(out,`ignored-${width}.png`),Buffer.from(shot.data,'base64'));
 }
 await run("document.querySelector('.restore-ignored').click()");
 await wait("document.querySelector('#ignored-face-review').textContent.includes('No ignored faces.')");
 assert.equal(await run("document.querySelector('#ignored-face-review h2').textContent"),'Ignored faces (0)');
 assert.equal(posts,1);
 await run("document.querySelector('#ignored-face-review>button').click()");
 await wait("document.querySelector('#ignored-face-review').hidden&&!document.querySelector('main').classList.contains('show-ignored')");
 await run("window.savedPeopleCard=document.querySelector('#groups .named-person-card');window.savedOtherCard=document.querySelector('#outliers .person-card')");
 await run("window.savedSuggestion=document.querySelectorAll('.suggestion-card')[1]");
 await run("[...document.querySelectorAll('.suggestion-actions button')].find(b=>b.textContent==='Not this person').click()");
 await wait("document.querySelector('#possible-matches summary>span').textContent==='Possible matches (1)'&&!document.querySelector('.suggestion-actions button').disabled");
 assert.equal(suggestionDecision.decision,'reject');
 assert.equal(await run("window.savedSuggestion===document.querySelector('.suggestion-card')"),true);
 await run("[...document.querySelectorAll('.suggestion-actions button')].find(b=>b.textContent==='Confirm match').click()");
 await wait("document.querySelector('#possible-matches summary>span').textContent==='Possible matches (0)'");
 assert.equal(suggestionDone,true);
 assert.equal(await run("window.savedPeopleCard===document.querySelector('#groups .named-person-card')&&window.savedOtherCard===document.querySelector('#outliers .person-card')"),true);
 groups.find(group=>group.id===2).faces.push({detection_id:9,media_id:9});
 await run("window.postMessage({type:'familyfoto-people-action',action:'refresh'},location.origin)");
 await wait("document.querySelector('#outliers .person-card').textContent.includes('2 photos')&&document.querySelector('#message').textContent.startsWith('Updated ')");
 assert.equal(await run("document.querySelector('#outliers').parentElement.open"),true);
 groups.push({id:6,person_id:null,name:null,section:'review',faces:[{detection_id:6,media_id:6}]});
 await run("window.postMessage({type:'familyfoto-people-action',action:'refresh'},location.origin)");
 await wait("!!document.querySelector('#review-groups [data-group=\"6\"] .group-photo')");
 assert.equal(await run("document.querySelectorAll('#review-groups input[name=name],#outliers input[name=name]').length"),0);
 await run("document.querySelector('#review-groups [data-group=\"6\"] .group-photo').click()");
 await wait("!!document.querySelector('#group-detail input[name=name]')");
 assert.equal(await run("document.querySelector('#group-detail').textContent.includes('Save name')"),true);
 console.log(JSON.stringify({ok:true,checks:['ignored list','desktop/mobile','restore revision and empty state'],artifacts:out}));
 await cdp('Browser.close');
}finally{ws?.close();chrome.kill();server.close();}
