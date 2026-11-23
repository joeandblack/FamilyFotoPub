import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {confirmAlbumTrash} from '../web/album-trash-warning.mjs';

test('unshared items need no confirmation',async()=>{
 assert.equal(await confirmAlbumTrash(async(url,options)=>{
  assert.equal(url,'/api/albums/membership');
  assert.equal(new Headers(options.headers).get('content-type'),'application/json');
  assert.deepEqual(JSON.parse(options.body),{media_ids:[1,2]});
  return {albums:[]};
 },[1,2],()=>{throw Error('Unexpected confirmation');}),true);
});
test('album viewer removal sends a JSON request',async()=>{
 const source=readFileSync(new URL('../web/viewer.mjs',import.meta.url),'utf8');
 const request=source.split('\n').find(line=>line.includes('await api(`/api/albums/${filters.album}/remove`'));
 assert.ok(request);
 let captured;
 await vm.runInNewContext(`(async()=>{${request}})()`,{
  filters:{album:9},m:{id:42},api:async(url,options)=>{captured={url,options};}
 });
 assert.equal(captured.url,'/api/albums/9/remove');
 const transport=new Request('https://example.test'+captured.url,captured.options);
 assert.equal(transport.headers.get('content-type'),'application/json');
 assert.deepEqual(await transport.json(),{media_ids:[42]});
});
test('shared items name affected albums and respect cancellation',async()=>{
 assert.equal(await confirmAlbumTrash(async()=>({albums:[{name:'Family'},{name:'Travel'}]}),[1],async message=>{
  assert.match(message,/Family, Travel/);assert.match(message,/every album/);return false;
 }),false);
});
test('membership lookup failure does not authorize trash',async()=>{
 await assert.rejects(confirmAlbumTrash(async()=>{throw Error('Offline');},[1]),/Offline/);
});
