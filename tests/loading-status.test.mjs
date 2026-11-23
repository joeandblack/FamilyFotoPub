import test from 'node:test';
import assert from 'node:assert/strict';
import {createLoadingStatus} from '../web/loading-status.mjs';
function fixture(){const element={dataset:{}};let later;const status=createLoadingStatus(element,{schedule:fn=>{later=fn;return 1;},cancel:()=>{}});return {element,status,tick:()=>later()};}
test('loading is immediate, slow requests get reassurance and completion clears it',()=>{
 const {element,status,tick}=fixture();const finish=status.start('Loading people...');
 assert.equal(element.hidden,false);assert.equal(element.dataset.state,'loading');tick();
 assert.match(element.textContent,/Still working/);finish();assert.equal(element.hidden,true);
});
test('old responses and timers cannot dismiss a newer request',()=>{
 const {element,status,tick}=fixture();const old=status.start('Old');const latest=status.start('New');old('old failure');
 assert.equal(element.textContent,'New');latest();tick();assert.equal(element.hidden,true);
});
test('errors stay visible and navigation clears loading',()=>{
 const {element,status}=fixture();status.start('Loading')('Network error');assert.equal(element.dataset.state,'error');assert.equal(element.hidden,false);
 const done=status.start('Retry');status.reset();done('late');assert.equal(element.hidden,true);
});
