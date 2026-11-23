import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const start=source.indexOf("window.addEventListener('message',async event=>{",source.indexOf('let peoplePhotoRequest=0'));
const handler=source.slice(start,source.indexOf('async function loadStorage()',start));

function fixture(){
 let listener,picker;
 const calls=[],replies=[],frame={postMessage:value=>replies.push(value)};
 const context={view:'people-preview',peoplePhotoRequest:0,previewPanel:{contentWindow:frame},location:{origin:'https://fixture'},
  window:{addEventListener:(_,fn)=>{listener=fn;}},
  albums:{add:(ids,options)=>{picker={ids,options};}},
  api:async path=>{calls.push(path);return {items:[{id:1},{id:2},{id:2}]};},notice:()=>{}};
 vm.runInNewContext(handler,context);
 return {calls,replies,get picker(){return picker;},open:mediaIds=>listener({origin:context.location.origin,source:frame,
  data:{type:'familyfoto-add-person-album',groupId:7,requestId:12,...(mediaIds===undefined?{}:{mediaIds})}})};
}

test('People opens album picker before any photo request; resolving is deferred',async()=>{
 const f=fixture();await f.open([2]);
 assert.equal(f.calls.length,0);assert(f.picker);assert.equal(f.replies.at(-1).opened,true);
 assert.equal(f.picker.options.refreshAfter,false);
 assert.deepEqual(Array.from(await f.picker.options.resolveIds()),[2]);
 assert.deepEqual(f.calls,['/api/people-preview/groups/7/photos']);
 f.picker.options.onSaved();assert.equal(f.replies.at(-1).albumSaved,true);
});

test('all-pages addition resolves distinct group photos only on submission',async()=>{
 const f=fixture();await f.open();assert.equal(f.calls.length,0);
 assert.deepEqual(Array.from(await f.picker.options.resolveIds()),[1,2]);
});

test('changed selection fails validation without a successful completion reply',async()=>{
 const f=fixture();await f.open([3]);assert.equal(f.calls.length,0);
 await assert.rejects(f.picker.options.resolveIds(),/Selected photos changed/);
 assert(!f.replies.some(r=>r.albumSaved));
});
