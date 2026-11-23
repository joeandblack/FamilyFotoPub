import test from 'node:test';
import assert from 'node:assert/strict';
import {readAlbumDestination,rememberAlbumDestination} from '../web/albums.mjs';

test('remembered destination survives reopening and excludes unavailable albums',()=>{
 const values=new Map(),storage={getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value)};
 const albums=[{id:1},{id:2}];
 assert.equal(readAlbumDestination(albums,null,storage),'new');
 rememberAlbumDestination(2,storage);
 assert.equal(readAlbumDestination(albums,null,storage),'2');
 assert.equal(readAlbumDestination(albums,2,storage),'new');
 assert.equal(readAlbumDestination([{id:1}],null,storage),'new');
 rememberAlbumDestination('new',storage);
 assert.equal(readAlbumDestination(albums,null,storage),'2');
 rememberAlbumDestination(3,storage);
 assert.equal(readAlbumDestination([...albums,{id:3}],null,storage),'3');
});
test('unavailable browser storage never prevents album actions',()=>{
 const storage={getItem(){throw Error('Denied');},setItem(){throw Error('Denied');}};
 assert.equal(readAlbumDestination([{id:1}],null,storage),'new');
 assert.doesNotThrow(()=>rememberAlbumDestination(1,storage));
});
