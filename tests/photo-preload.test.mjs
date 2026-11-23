import test from 'node:test';
import assert from 'node:assert/strict';
import {createPhotoPreloader,mediaURL} from '../web/photo-preload.mjs';
const photo=id=>({id,kind:'image',name:`${id}.jpg`,relative_path:`${id}.jpg`});
function fixture(){const images=[];return {images,cache:createPhotoPreloader(()=>{const image={decode:async()=>{}};images.push(image);return image;})};}
test('only the next two items are preloaded; video downloads are excluded',()=>{
 const {cache,images}=fixture();cache.warm([photo(1),{...photo(2),kind:'video'},photo(3)]);assert.equal(images.length,1);
 assert.match(images[0].src,/\/1\/original$/);assert.match(mediaURL({...photo(4),relative_path:'a.HEIC'}),/preview$/);
});
test('navigation reuses the same decoded element and sliding window evicts old entries',async()=>{
 const {cache,images}=fixture();cache.warm([photo(1),photo(2)]);
 const ready=cache.load(photo(1));let decoded=false;images[0].decode=async()=>{decoded=true;};await images[0].onload();assert.equal(await ready,images[0]);assert.equal(decoded,true);
 cache.warm([photo(2),photo(3)]);assert.equal(images.length,3);cache.load(photo(1));assert.equal(images.length,4);
 cache.clear();cache.load(photo(2));assert.equal(images.length,5);
});
test('failed preload stays quiet until requested and decoding is awaited',async()=>{
 const {cache,images}=fixture();cache.warm([photo(1)]);images[0].onerror();await assert.rejects(cache.load(photo(1)),/Preview unavailable/);
 cache.clear();let finish;const p=cache.load(photo(2));images[1].decode=()=>new Promise(r=>finish=r);images[1].onload();let ready=false;p.then(()=>ready=true);await Promise.resolve();assert.equal(ready,false);finish();await p;assert.equal(ready,true);
});
