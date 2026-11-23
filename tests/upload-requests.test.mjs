import test from 'node:test';
import assert from 'node:assert/strict';
import {uploadRequest,sendChunk,handoff,bounded,UploadTimeout} from '../web/upload-requests.mjs';
const never=()=>new Promise(()=>{});
test('timeout covers both hung fetch and hung response JSON even when abort is ignored',async()=>{
 for(const fetcher of [never,async()=>({ok:true,json:never})])
  await assert.rejects(uploadRequest('',{timeoutMs:15},fetcher),UploadTimeout);
});
test('pause aborts response reading and bounded hashing immediately',async()=>{
 const c=new AbortController();const request=uploadRequest('',{signal:c.signal,timeoutMs:1000},async()=>({ok:true,json:never}));c.abort();
 await assert.rejects(request,{name:'AbortError'});
 const h=new AbortController();const pending=bounded(never,{signal:h.signal});h.abort();await assert.rejects(pending,{name:'AbortError'});
});
const options={sid:'session',fileId:56,offset:8,end:12,blob:new Blob(['last']),sha256:'fixture',wait:async()=>{}};
test('lost chunk response reconciles success without writing the chunk twice',async()=>{
 const calls=[];await sendChunk({...options,request:async(path,init)=>{calls.push(init?.method||'GET');if(init?.method==='PUT')throw new UploadTimeout();return {state:'uploading',files:[{file_id:56,received:12}]};}});
 assert.deepEqual(calls,['PUT','GET']);
});
test('unchanged offset permits bounded retry; unknown or partial offset never permits blind resend',async()=>{
 let puts=0;await sendChunk({...options,request:async(path,init)=>{if(init?.method==='PUT'){if(++puts===1)throw new UploadTimeout();return {received:12};}return {state:'uploading',files:[{file_id:56,received:8}]};}});assert.equal(puts,2);
 for(const received of [9,0]){
  puts=0;await assert.rejects(sendChunk({...options,request:async(path,init)=>{if(init?.method==='PUT'){puts++;throw new UploadTimeout();}return {state:'uploading',files:[{file_id:56,received}]};}}),/offset changed/);assert.equal(puts,1);
 }
 puts=0;await assert.rejects(sendChunk({...options,request:async(path,init)=>{if(init?.method==='PUT')puts++;throw new UploadTimeout();}}),UploadTimeout);assert.equal(puts,1);
});
test('repeated failures stop after three writes; cancellation prevents reconciliation/retry',async()=>{
 let puts=0;await assert.rejects(sendChunk({...options,request:async(path,init)=>{if(init?.method==='PUT'){puts++;throw Error('offline');}return {state:'uploading',files:[{file_id:56,received:8}]};}}),/bounded retries/);assert.equal(puts,3);
 const c=new AbortController();let count=0;await assert.rejects(sendChunk({...options,signal:c.signal,request:async()=>{count++;c.abort();throw Error('stopped');}}),{name:'AbortError'});assert.equal(count,1);
});
test('lost finalize response recovers durable handoff without duplicate finalization',async()=>{
 let posts=0;const result=await handoff({sid:'s',request:async(path,init)=>{if(init?.method==='POST'){posts++;throw new UploadTimeout();}return {state:'queued',job_id:13,handoff_ready:true};}});
 assert.equal(posts,1);assert.equal(result.job_id,13);
});
test('unconfirmed handoff and bounded reconciliation cannot claim safe disconnect',async()=>{
 await assert.rejects(handoff({sid:'s',request:async()=>{throw new UploadTimeout();}}),UploadTimeout);
 let posts=0;await assert.rejects(handoff({sid:'s',wait:async()=>{},request:async(path,init)=>{if(init?.method==='POST'){posts++;return {job_id:13,handoff_ready:false};}return {state:'uploading',handoff_ready:false};}}),/Handoff not confirmed/);assert.equal(posts,3);
});
