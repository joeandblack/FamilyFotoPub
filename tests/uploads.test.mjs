import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {SHA256} from '../web/sha256.mjs';
import {selectionManifest,sameSelection,visibilityNote} from '../web/uploads.mjs';
import {createUploadFeedback,uploadMessage,uploadBytes} from '../web/upload-feedback.mjs';

function feedbackFixture(storage=new Map()){
 let text='';const adapter={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
 return {feedback:createUploadFeedback(value=>text=value,adapter),text:()=>text,storage};
}
const session=(state,id='one',error=null)=>({id,state,label:'Holiday',job_id:12,error});
test('helper follows queued, importing, done, paused, failure and cancellation states',()=>{
 const {feedback:f,text}=feedbackFixture();f.select('one');
 for(const [state,expected] of [['queued','queued'],['importing','importing / processing'],['done','import complete'],['paused','paused'],['failed','needs attention'],['cancelling','cancelling'],['cancelled','cancelled']]){
  assert(f.accept([session(state)],f.request()));assert(text().includes(expected));
  if(state!=='queued')assert(!text().includes('queued'));
 }
 assert(uploadMessage(session('failed','one','Disk unavailable')).includes('Disk unavailable'));
 assert(uploadMessage(session('done','one','Video stage needs review')).includes('Video stage needs review'));
});
test('safe-to-disconnect text requires a confirmed durable server handoff',()=>{
 assert(!uploadMessage(session('queued')).includes('Safe to close'));
 assert(uploadMessage({...session('queued'),handoff_ready:true}).includes('Safe to close'));
 assert(uploadMessage({...session('done'),handoff_ready:true,staging_cleaned:true}).includes('Temporary copies removed'));
});
test('late polls and action responses cannot overwrite a newer selection or fresher status',()=>{
 const {feedback:f,text}=feedbackFixture();const oldSelection=f.select('one');const oldPoll=f.request();
 const newest=f.request();f.accept([session('done')],newest);assert(!f.accept([session('queued')],oldPoll));assert(text().includes('complete'));
 const pending=f.request();f.select();const selected=text();
 assert(!f.accept([session('done')],pending));f.state(session('queued'),oldSelection);f.error('Old failure',oldSelection);assert.equal(text(),selected);
 f.accept([session('done')],f.request());assert.equal(text(),selected); // Reselection is not auto-bound to the last upload.
});
test('refresh restores the selected upload, with newest-session fallback for older clients',()=>{
 const {feedback:f,storage}=feedbackFixture();f.select('one');
 const restored=feedbackFixture(storage);restored.feedback.accept([session('queued','two'),session('done')],restored.feedback.request());
 assert(restored.text().includes('complete'));
 const fresh=feedbackFixture();fresh.feedback.accept([session('done')],fresh.feedback.request());assert(fresh.text().includes('complete'));
});
test('polling preserves transfer progress and actionable local errors; final responses invalidate older polls',()=>{
 const {feedback:f,text}=feedbackFixture();const token=f.select('one');f.show('Checking bytes',token);
 f.accept([session('uploading')],f.request(),true);assert.equal(text(),'Checking bytes');
 f.error('Reselected content differs',token);f.accept([session('uploading')],f.request());assert.equal(text(),'Reselected content differs');
 const old=f.request();f.state(session('done'),token);assert(!f.accept([session('queued')],old));assert(text().includes('complete'));
});
test('HTTP incremental SHA-256 matches independent implementation at block boundaries',()=>{
 for(const length of [0,1,55,56,63,64,65,1000,1000000]){
  const input=Uint8Array.from({length},(_,i)=>i%251);const h=new SHA256();
  for(let i=0;i<input.length;i+=37)h.update(input.subarray(i,i+37));
  assert.equal(h.hex(),createHash('sha256').update(input).digest('hex'));
 }
});
test('reselect uses canonical relative Unicode paths and immutable manifest',()=>{
 const files=[{webkitRelativePath:'Holiday/日本/été.jpg',size:7,lastModified:123},{webkitRelativePath:'Holiday/a.jpg',size:8,lastModified:124}];
 const m=selectionManifest(files);const session={files:m.map(f=>({relative_path:f.path,size:f.size,modified_ms:f.modified}))};
 assert(sameSelection(selectionManifest(files.toReversed()),session));
 assert(!sameSelection([{...m[0],size:9},m[1]],session));
 assert(!sameSelection(m.slice(1),session));
});

test('byte progress restores acknowledged accepted bytes and bounds invalid values',()=>{
 const resumed={total_bytes:200,files:[{disposition:'accepted',received:50},{disposition:'ignored',received:90}]};
 assert.equal(uploadBytes(resumed).percent,25);
 assert.match(uploadBytes(resumed).text,/25.0% transferred/);
 assert.equal(uploadBytes({total_bytes:100,received:99.99}).percent,99.9);
 assert.equal(uploadBytes({total_bytes:100,received:500}).percent,100);
 for(const received of [-2,NaN,Infinity])assert.equal(uploadBytes({total_bytes:100,received}).percent,0);
 for(const total_bytes of [0,NaN,Infinity,-1])assert.match(uploadBytes({total_bytes}).text,/100.0%.*no bytes to transfer/);
});
test('100 percent transfer is not verification or permission to disconnect; stale progress is ignored',()=>{
 const full={...session('uploading'),total_bytes:10,received:10};
 assert.match(uploadMessage(full),/100.0% transferred/);
 assert.match(uploadMessage(full),/handoff not confirmed/);
 assert.doesNotMatch(uploadMessage(full),/Safe to close/);
 const {feedback:f,text}=feedbackFixture();f.select('one');const old=f.request(),fresh=f.request();
 f.accept([{...full,received:7}],fresh);f.accept([{...full,received:3}],old);
 assert.match(text(),/70.0%/);assert.doesNotMatch(text(),/30.0%/);
});

test('the batch label follows current privacy, not the choice made at upload time',()=>{
 const counts={shared:1,private:0};
 assert.equal(visibilityNote({visibility:'shared',requested_visibility:'private',visibility_counts:counts,state:'done'}),'');
 assert.equal(visibilityNote({visibility:'private',requested_visibility:'shared',visibility_counts:{shared:0,private:2},state:'done'}),' · Private');
 assert.match(visibilityNote({visibility:'mixed',visibility_counts:{shared:1,private:3},state:'done'}),/Mixed \(3 private, 1 shared\)/);
 assert.equal(visibilityNote({visibility:null,requested_visibility:'private',state:'uploading'}),' · Private once imported');
 assert.equal(visibilityNote({visibility:null,requested_visibility:'private',state:'done'}),'');
});
