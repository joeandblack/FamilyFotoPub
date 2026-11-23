import test from 'node:test';
import assert from 'node:assert/strict';
import {runPeopleReview,reviewProgressText,createPeopleActionProgress} from '../face_detect/web/people-review-progress.mjs';

const response=chunks=>new Response(new ReadableStream({start(controller){for(const chunk of chunks)controller.enqueue(new TextEncoder().encode(chunk));controller.close();}}));
test('Detect again streams waiting and comparison through its job endpoint',async()=>{
 const phases=[];
 const result=await runPeopleReview('/api/people-preview',{revision:3},event=>phases.push(event.phase),{
  path:'/jobs/completed-imports/recheck/stream',fetcher:async url=>{
   assert.equal(url,'/api/people-preview/jobs/completed-imports/recheck/stream');
   return response(['{"state":"running","phase":"waiting"}\n{"state":"running","phase":"comparing"}\n{"state":"completed","result":{"matched":2}}\n']);
  }
 });
 assert.deepEqual(phases,['waiting','comparing']);assert.equal(result.matched,2);
});
test('split streamed messages report real progress and wait for saved result',async()=>{
 const updates=[];let calls=0;
 const result=await runPeopleReview('/api/people-preview',{action:'match'},m=>updates.push(m),{fetcher:async(url,options)=>{
  calls++;assert.equal(url,'/api/people-preview/review-actions/stream');assert.equal(JSON.parse(options.body).action,'match');
  return response(['{"state":"running","phase":"wait','ing"}\n{"state":"running","phase":"processing","done":4,"total":8}\n',
   '{"state":"running","phase":"saving"}\n{"state":"completed","result":{"matched":8}}\n']);
 }});
 assert.equal(calls,1);assert.equal(result.matched,8);assert.equal(updates.length,3);assert.equal(updates[1].done,4);
});
test('truncated stream never claims success or retries the action',async()=>{
 let calls=0;
 await assert.rejects(runPeopleReview('/api',{},()=>{},{fetcher:async()=>{calls++;return response(['{"state":"running","phase":"saving"}\n']);}}),/may still be running or saved/);
 assert.equal(calls,1);
});
test('server failure preserves the reason',async()=>{
 await assert.rejects(runPeopleReview('/api',{},()=>{},{fetcher:async()=>response(['{"state":"failed","message":"Selected faces changed"}\n'])}),/Selected faces changed/);
});
test('large affected group updates are accepted without resubmitting',async()=>{
 const groups=[{id:1,faces:Array.from({length:18000},(_,id)=>({detection_id:id,media_id:id,origin:'automatic'}))}];
 const line=JSON.stringify({state:'completed',result:{updates:{groups}}})+'\n';
 assert(line.length>1024*1024);
 const result=await runPeopleReview('/api',{},()=>{},{fetcher:async()=>response([line])});
 assert.equal(result.updates.groups[0].faces.length,18000);
});
test('progress distinguishes waiting, processing, saving and refresh',()=>{
 assert.equal(reviewProgressText({phase:'waiting'},'Matching groups...',5),'Working...');
 assert.match(reviewProgressText({phase:'processing',done:12,total:72},'Matching groups...',9),/12 \/ 72 faces/);
 assert.match(reviewProgressText({phase:'saving'},'Matching groups...',10),/Saving changes/);
 assert.match(reviewProgressText({phase:'refreshing'},'Matching groups...',11),/Updating/);
});

test('shared indicator has honest counts and resets after success or failure',()=>{
 const old=globalThis.document;
 const element=()=>({style:{},children:[],attributes:{},classList:{toggle(){}},append(...nodes){this.children.push(...nodes);},before(node){this.beforeNode=node;},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this[k];}});
 globalThis.document={createElement:element};
 try{
  const status=element(),indicator=createPeopleActionProgress(status),bar=status.beforeNode.children[1];
  indicator.start('Ignoring 3 selected faces...');assert.equal(bar.hidden,false);assert.equal(bar.value,undefined);
  indicator.report({phase:'processing',done:1,total:3});assert.equal(bar.value,1);assert.equal(bar.max,3);assert.match(status.textContent,/1 \/ 3 faces/);
  indicator.report({phase:'saving'});assert.equal(bar.value,undefined);assert.equal(status.textContent,'Saving changes...');
  indicator.fail(Error('Not saved'));assert.equal(bar.hidden,true);assert.equal(status.textContent,'Not saved');
  indicator.start('Moving face...');assert.equal(bar.hidden,false);assert.equal(status.style.color,'#68766f');
  indicator.finish();assert.equal(bar.hidden,true);assert.equal(status.textContent,'');
 }finally{globalThis.document=old;}
});
