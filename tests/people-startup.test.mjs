import test from 'node:test';
import assert from 'node:assert/strict';
import {createPeopleStartup} from '../face_detect/web/people-startup.mjs';
const storage=()=>{let data;return {getItem:()=>data,setItem:(_,value)=>{data=value;}};};
const response=(status,data={})=>({status,ok:status<400,json:async()=>data});

test('explicit resume starts a stopped service before checking and resuming its job',async()=>{
 const calls=[];let online=false,started=false;
 const controller=createPeopleStartup({storage:storage(),sleep:async()=>{},request:async(url,options)=>{
  calls.push(url);
  if(url==='/api/face-service')return response(200,{state:started?'ready':'stopped'});
  if(url==='/api/face-service/start'){started=true;online=true;return response(202);}
  if(!online)return response(503);
  return response(200,options?{}:{state:'paused'});
 }});
 await controller.command(3,'resume',()=>{});
 assert.deepEqual(calls,['/api/people-preview/jobs/import-3','/api/face-service','/api/face-service/start',
  '/api/face-service','/api/people-preview/jobs/import-3','/api/people-preview/jobs/import-3/control']);
});

test('service startup failure is shown without submitting the scan',async()=>{
 let started=false;
 const controller=createPeopleStartup({storage:storage(),sleep:async()=>{},request:async(url)=>{
  if(url==='/api/face-service')return response(200,{state:started?'unavailable':'stopped',message:'Model is missing'});
  if(url==='/api/face-service/start'){started=true;return response(202);}
  assert.equal(url,'/api/people-preview/jobs/import-3');return response(503);
 }});
 await controller.command(3,'resume',()=>{});
 assert.equal(controller.merge({})[3].state,'start-error');
 assert.equal(controller.merge({})[3].message,'Model is missing');
});

test('pause never starts a stopped service and preserves the useful error',async()=>{
 const controller=createPeopleStartup({storage:storage(),request:async(url)=>{
  assert.equal(url,'/api/people-preview/jobs/import-3');return response(503,{detail:'Face service is stopped'});
 }});
 await controller.command(3,'pause',()=>{});
 assert.equal(controller.merge({})[3].message,'Face service is stopped');
});

test('resume waits for an already starting service without starting it twice',async()=>{
 let polls=0,checks=0,controls=0;
 const controller=createPeopleStartup({storage:storage(),sleep:async()=>{},request:async(url,options)=>{
  if(url==='/api/face-service')return response(200,++polls<3?{state:'starting',busy:true}:{state:'ready'});
  if(options){assert.equal(url,'/api/people-preview/jobs/import-3/control');controls++;return response(200);}
  return ++checks===1?response(503):response(200,{state:'paused'});
 }});
 await controller.command(3,'resume',()=>{});
 assert.equal(polls,3);assert.equal(controls,1);
});
test('large startup is immediately visible, never submits twice, and reconciles timeout',async()=>{
 const saved=storage(),calls=[];let finish;
 const controller=createPeopleStartup({storage:saved,request:async(url,options)=>{calls.push(url);return options?new Promise(r=>finish=r):response(404);}});
 const running=controller.command(1,'resume',()=>{});
 assert.equal(controller.merge({1:{state:'not-started',total:46588}})[1].state,'preparing');
 await controller.command(1,'resume',()=>{});
 await Promise.resolve();assert.equal(calls.length,2);
 finish(response(504));await running;
 const restored=createPeopleStartup({storage:saved,request:()=>assert.fail('Do not resubmit')});
 assert.equal(restored.merge({1:{state:'not-started',total:46588}})[1].state,'preparing');
 assert.equal(restored.merge({1:{state:'running',done:34,total:46588}})[1].done,34);
});
test('definite rejection is inline and allows explicit retry',async()=>{
 const controller=createPeopleStartup({storage:storage(),request:async(_,options)=>options?response(409,{detail:'Another operation is running'}):response(404)});
 await controller.command(1,'resume',()=>{});
 assert.equal(controller.merge({})[1].state,'start-error');
 assert.match(controller.merge({})[1].message,/Another operation/);
});
test('resume of an active scan does not dispatch another worker',async()=>{
 let calls=0;const controller=createPeopleStartup({storage:storage(),request:async()=>{calls++;return response(200,{state:'running'});}});
 await controller.command(1,'resume',()=>{});assert.equal(calls,1);
 assert.equal(controller.merge({1:{state:'running'}})[1].state,'running');
});

for(const state of ['waiting-service','awaiting-receipt','cancelled'])test(`startup reveals ${state} instead of masking it`,async()=>{
 const saved=storage();const controller=createPeopleStartup({storage:saved,request:async(_,options)=>response(options?200:404)});
 await controller.command(22,'resume',()=>{});
 assert.equal(controller.merge({22:{state,total:1296}})[22].state,state);
 const restored=createPeopleStartup({storage:saved,request:()=>assert.fail()});
 assert.equal(restored.merge({22:{state}})[22].state,state);
});

test('polling can reconcile a job before its startup response returns',async()=>{
 let finish;const controller=createPeopleStartup({storage:storage(),request:async(_,options)=>options?new Promise(resolve=>finish=resolve):response(404)});
 const command=controller.command(22,'resume',()=>{});await Promise.resolve();
 controller.merge({22:{state:'waiting-service'}});finish(response(200));await command;
 assert.equal(controller.merge({22:{state:'waiting-service'}})[22].state,'waiting-service');
});
