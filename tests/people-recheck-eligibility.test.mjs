import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../face_detect/web/automatic-people-preview.mjs',import.meta.url),'utf8');
const code=source.slice(source.indexOf('async function updateRecheckEligibility(){'),source.indexOf('if(integrated)updateRecheckEligibility();'));
test('Detect again is repeatable and only disabled during its own request',async()=>{
 const context=vm.createContext({state:null,completedScan:null,recheckBusy:false,recheck:{},reviewRecheck:{},recheckReason:{},request:()=>{throw Error('Status checks must not gate the button');}});
 vm.runInContext(code,context);
 await context.updateRecheckEligibility();assert.equal(context.recheck.disabled,false);
 assert.equal(context.completedScan,'completed-imports');
 for(let n=0;n<3;n++){
  context.recheckBusy=true;await context.updateRecheckEligibility();assert.equal(context.recheck.disabled,true);assert.equal(context.reviewRecheck.disabled,true);
  context.recheckBusy=false;await context.updateRecheckEligibility();assert.equal(context.recheck.disabled,false);assert.equal(context.reviewRecheck.disabled,false);
 }
});

test('review and Other photos share one operation and result message',()=>{
 assert(source.includes('reviewRecheck.onclick=()=>recheck.onclick()'));
 assert(source.includes('for(const indicator of recheckIndicators)indicator.finish(text)'));
 assert(source.includes('if(!completedScan||recheckBusy)return'));
});
