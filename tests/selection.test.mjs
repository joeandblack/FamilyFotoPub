import test from 'node:test';
import assert from 'node:assert/strict';
import {rectangle,intersects,contentPoint,dragMode,dragSelection,rangeSelection,downloadLinks} from '../web/selection.mjs';
import {changeSelected} from '../web/trash-selection.mjs';

test('drag geometry works in either direction and keeps scroll content coordinates',()=>{
 assert.deepEqual(rectangle({x:50,y:90},{x:10,y:20}),{left:10,top:20,right:50,bottom:90});
 const point=contentPoint({x:30,y:80},{left:10,top:50},{left:0,top:300});
 assert.deepEqual(point,{x:20,y:330});
 assert(intersects(rectangle({x:0,y:300},point),{left:10,top:320,right:40,bottom:350}));
 assert(!intersects({left:0,top:0,right:10,bottom:10},{left:10,top:0,right:20,bottom:10}));
});
test('modifier drags use a stable baseline and shift click follows loaded order',()=>{
 const base=new Set([1,2]);
 assert.deepEqual([...dragSelection(base,[2,3],'replace')],[2,3]);
 assert.deepEqual([...dragSelection(base,[2,3],'add')],[1,2,3]);
 assert.deepEqual([...dragSelection(base,[2,3],'toggle')],[1,3]);
 assert.deepEqual([...base],[1,2]);
 assert.deepEqual(rangeSelection([7,4,9,2],9,7),[7,4,9]);
});
test('a plain drag replaces by default and adds where a click adds',()=>{
 assert.equal(dragMode({}),'replace');
 assert.equal(dragMode({},'add'),'add');
 assert.equal(dragMode({shiftKey:true}),'add');
 for(const key of ['ctrlKey','metaKey'])assert.equal(dragMode({[key]:true},'add'),'toggle');
 // The unsteady click: a short drag over one more group keeps the earlier ones.
 assert.deepEqual([...dragSelection(new Set([1,2]),[3],dragMode({},'add'))],[1,2,3]);
});
test('downloads deduplicate IDs, stay within loaded rows and disambiguate bulk filenames',()=>{
 const rows=[{id:1,name:'same.JPG'},{id:2,name:'same.jpg'},{id:1,name:'same.JPG'}];
 const links=downloadLinks(rows,new Set([1,2,999]));
 assert.deepEqual(links.map(x=>x.url),['/api/media/1/download?disambiguate=true','/api/media/2/download?disambiguate=true']);
 assert.equal(downloadLinks(rows,new Set([1]))[0].url,'/api/media/1/download');
});
test('Library bulk delete uses recoverable Trash and stops at a partial failure',async()=>{
 const calls=[],done=[];
 const result=await changeSelected([1,1,2,3],async url=>{calls.push(url);if(url.includes('/2/'))throw Error('Video maintenance active');},'trash',id=>done.push(id));
 assert.deepEqual(calls,['/api/media/1/trash/trash','/api/media/2/trash/trash']);
 assert.deepEqual(done,[1]);assert.equal(result.deleted,1);assert.match(result.error,/maintenance/);
});
test('changing filter context stops remaining bulk commands',async()=>{
 let active=true;const calls=[];
 const result=await changeSelected([1,2],async url=>{calls.push(url);active=false;},'trash',()=>{},()=>active);
 assert.equal(calls.length,1);assert.match(result.error,/context changed/);
});
