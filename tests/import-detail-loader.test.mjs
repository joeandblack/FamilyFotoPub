import test from 'node:test';
import assert from 'node:assert/strict';
import {createImportDetailLoader} from '../web/import-detail-loader.mjs';
test('details load on demand, coalesce requests and expire after refresh interval',async()=>{
 const calls=[];let clock=0;
 const load=createImportDetailLoader(async path=>{calls.push(path);return path.includes('issues')?{imports:[]}:{imports:[{id:22}]};},()=>clock);
 assert.equal(calls.length,0);
 const first=load(22);assert.equal(load(22),first);
 assert.deepEqual((await first).issues,{imports:[]});
 assert.equal(calls.length,2);await load(22);assert.equal(calls.length,2);
 clock=5000;await load(22);assert.equal(calls.length,4);
 assert(calls.every(p=>p.includes('job_id=22')));
});
