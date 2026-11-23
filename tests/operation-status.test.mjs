import test from 'node:test';
import assert from 'node:assert/strict';
import {operationStatus,operationIndicator} from '../web/operation-status.mjs';
import {readFileSync} from 'node:fs';

test('unknown People status never claims the app is idle during startup',()=>{
 const pending={peopleReady:false};
 assert.equal(operationStatus({people:null},pending).label,'Initializing...');
 assert.equal(operationStatus({job:{state:'completed'},people:null},pending).label,'Initializing...');
 assert.equal(operationStatus({job:{state:'running'},people:null},pending).label,'Importing');
 assert.equal(operationStatus({people:{1:{state:'running'}}},{peopleReady:true}).label,'Discover people');
 assert.equal(operationStatus({people:{}},{peopleReady:true}).label,'Ready');
 const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
 assert.match(html,/id="sideState">Initializing\.\.\.</);
});

test('people scan stays active while yielding and stops on completion or pause',()=>{
 for(const state of ['queued','running','waiting-for-foreground','awaiting-receipt']){
  assert.equal(operationStatus({people:{21:{state}}}).label,'Discover people');
 }
 for(const state of ['paused','completed','cancelled'])assert.equal(operationStatus({people:{21:{state}}}).status,null);
 assert.equal(operationStatus({people:{21:{state:'blocked'}}}).stage,'people');
 assert.equal(operationStatus({job:{state:'running'},people:{21:{state:'waiting-for-foreground'}}}).stage,'import');
});
test('all four worker stages have distinct short labels',()=>{
 for(const [key,label] of [['job','Importing'],['video','Process videos'],['conversion','Convert videos'],['duplicates','Scan dups']]){
  const status={state:'running'};
  assert.equal(operationStatus({[key]:status}).label,label);
  assert.equal(operationStatus({[key]:status}).status,status);
 }
});
test('completed and paused workers do not show as running',()=>{
 assert.equal(operationStatus({duplicates:{state:'completed-review-required'},job:{state:'paused'}}).label,'Ready');
 assert.equal(operationStatus({}).status,null);
});
test('active worker takes priority over historical error, otherwise error remains visible',()=>{
 const error={state:'paused-error',error:'checksum'};
 assert.equal(operationStatus({conversion:error,duplicates:{state:'running'}}).stage,'duplicates');
 assert.equal(operationStatus({duplicates:error}).status,error);
});

test('ready is steady green while initialization is unknown, not ready',()=>{
 assert.equal(operationIndicator(operationStatus({})).state,'ready');
 assert.equal(operationIndicator(operationStatus({},{peopleReady:false})).state,'idle');
 const css=readFileSync(new URL('../web/styles.css',import.meta.url),'utf8');
 assert.match(css,/\.dot\.ready\{background:#25864d;animation:none\}/);
});

test('waiting for service, failures and stale monitor never claim readiness',()=>{
 const waiting=operationIndicator(operationStatus({people:{1:{state:'waiting-service'}}}));
 assert.equal(waiting.state,'waiting');assert.equal(waiting.label,'Waiting for service');
 for(const state of ['blocked','paused-error','failed','error'])assert.equal(operationIndicator(operationStatus({people:{1:{state}}})).state,'error');
 const operation=operationStatus({conversion:{state:'running'}});
 assert.equal(operationIndicator(operation,{state:'healthy',updated:'2026-10-28T10:00:00Z'},Date.parse('2026-10-28T10:00:10Z')).state,'running');
 assert.equal(operationIndicator(operation,{state:'healthy',updated:'2026-10-28T10:00:00Z'},Date.parse('2026-10-28T10:02:00Z')).label,'Check status');
 assert.equal(operationIndicator(operationStatus({people:{1:{state:'waiting-for-foreground'}}})).state,'running');
});
