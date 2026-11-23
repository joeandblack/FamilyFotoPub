import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeNavigation,readNavigation,saveNavigation,importNavigation} from '../web/navigation.mjs';
import {importEstimate,estimateLabel,createEstimateDisplay} from '../web/progress.mjs';

test('navigation restores section, filters and page; rejects malformed values',()=>{
 let saved=null;const storage={getItem:()=>saved,setItem:(key,value)=>{saved=value;}};
 const state={view:'image',year:'2020',search:'trip',page:3,import_job:'14'};
 saveNavigation(storage,state);assert.deepEqual(readNavigation(storage),state);
 assert.equal(normalizeNavigation({view:'storage'}).view,'settings');
 assert.equal(normalizeNavigation({view:'settings'}).view,'settings');
 saved='{bad';assert.deepEqual(readNavigation(storage),{view:'all',year:'all',search:'',page:1,import_job:'all'});
 assert.equal(normalizeNavigation({page:-1,year:'oops'}).year,'all');
});
test('estimate uses active time and slower file/byte projection; suppresses early/paused/completed estimates',()=>{
 const j={state:'running',inventory_complete:true,active_seconds:120,processed:20,total:100,processed_bytes:800,total_bytes:1000};
 assert.equal(importEstimate(j,0).remainingSeconds,480);
 assert.equal(importEstimate({...j,processed_bytes:100},0).remainingSeconds,1080);
 for(const state of ['paused','scanning','paused-error','completed-review-required'])assert.equal(importEstimate({...j,state}),null);
 assert.equal(importEstimate({...j,active_seconds:30}),null);
 assert.equal(importEstimate({...j,processed:100}),null);
 assert.equal(importEstimate({...j,active_seconds:NaN}),null);
});

test('estimate updates every two minutes but reacts immediately to pause, faults and new jobs',()=>{
 const display=createEstimateDisplay(),now=new Date(2026,9,10,12).getTime();
 const job={id:5,state:'running',inventory_complete:true,active_seconds:120,processed:20,total:100,processed_bytes:800,total_bytes:1000};
 const first=display(job,true,now);
 const changed={...job,active_seconds:1200};
 assert.equal(display(changed,true,now+119999),first);
 assert.notEqual(display(changed,true,now+120000),first);
 assert.equal(display({...job,state:'paused'},true,now+120001),'Estimate paused');
 assert.equal(display(job,false,now+120002),'');
 assert.equal(display({...job,id:6,state:'completed-review-required'},true,now+120003),'Import complete');
});

test('estimate label shows compact remaining duration',()=>{
 const now=new Date(2026,9,10,12).getTime();
 assert.equal(estimateLabel(now+3600000,now),'Est. 1hr 0m left');
 assert.equal(estimateLabel(now+(6*60+34)*60000,now),'Est. 6hr 34m left');
 assert.equal(estimateLabel(now+27*3600000,now),'Est. 27hr 0m left');
 assert.doesNotMatch(estimateLabel(now+3600000,now),/remaining|2026|Oct/);
});

test('import navigation clears old filters and validates restored import IDs',()=>{
 assert.deepEqual(importNavigation(14),{view:'all',page:1,year:'all',search:'',import_job:'14'});
 for(const value of ['0','-1','abc','Infinity','9007199254740992'])assert.equal(normalizeNavigation({import_job:value}).import_job,'all');
});
