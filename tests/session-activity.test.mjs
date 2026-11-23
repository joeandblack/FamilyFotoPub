import test from 'node:test';
import assert from 'node:assert/strict';
import {trackSessionActivity} from '../web/session-activity.mjs';
test('only trusted visible interaction renews sessions, throttled across event types',async()=>{
 const handlers=new Map();let clock=0,calls=0;
 const target={visibilityState:'visible',addEventListener:(name,fn)=>handlers.set(name,fn),removeEventListener:name=>handlers.delete(name)};
 const stop=trackSessionActivity(target,async()=>{calls++;},()=>clock);
 assert.equal(calls,0);
 await handlers.get('pointerdown')({isTrusted:false});assert.equal(calls,0);
 await handlers.get('keydown')({isTrusted:true});assert.equal(calls,1);
 await handlers.get('pointermove')({isTrusted:true});assert.equal(calls,1);
 clock=30000;target.visibilityState='hidden';
 await handlers.get('wheel')({isTrusted:true});assert.equal(calls,1);
 target.visibilityState='visible';await handlers.get('touchstart')({isTrusted:true});assert.equal(calls,2);
 stop();assert.equal(handlers.size,0);
});
test('backup modal interaction does not renew the stopped application session',async()=>{
 const handlers=new Map();let active=true,calls=0;
 const target={visibilityState:'visible',querySelector:()=>active?{}:null,addEventListener:(name,fn)=>handlers.set(name,fn),removeEventListener:()=>{}};
 const stop=trackSessionActivity(target,async()=>{calls++;});
 await handlers.get('wheel')({isTrusted:true});assert.equal(calls,0);
 active=false;await handlers.get('wheel')({isTrusted:true});assert.equal(calls,1);
 stop();
});
