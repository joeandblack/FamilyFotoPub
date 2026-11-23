import test from 'node:test';
import assert from 'node:assert/strict';
import {size} from '../web/size.mjs';
import {deleteSelected} from '../web/trash-selection.mjs';
test('UI units are decimal without changing raw byte values',()=>{
 assert.equal(size(1e9),'1.0 GB');assert.equal(size(1024**3),'1.1 GB');
 assert.equal(size(1e6),'1.0 MB');assert.equal(size(1024**2),'1.0 MB');
 assert.equal(size(999),'999 bytes');
});
test('batch deletion stops at first fault and retains remaining selections',async()=>{
 const calls=[],deleted=[];
 const outcome=await deleteSelected([1,2,3],async url=>{calls.push(url);if(url.includes('/2/'))throw Error('Checksum mismatch');},id=>deleted.push(id));
 assert.deepEqual(deleted,[1]);assert.equal(calls.length,2);
 assert.deepEqual(outcome,{deleted:1,error:'Checksum mismatch'});
});
test('successful batch uses the existing guarded per-item endpoint',async()=>{
 const calls=[];
 assert.deepEqual(await deleteSelected([4,5],async (url,options)=>{assert.equal(options.method,'POST');calls.push(url);},()=>{}),{deleted:2,error:null});
 assert.deepEqual(calls,['/api/media/4/trash/delete','/api/media/5/trash/delete']);
});
