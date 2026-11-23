import test from 'node:test';
import assert from 'node:assert/strict';
import {groupPage,syncGroupCards} from '../face_detect/web/people-group-pages.mjs';

test('card reconciliation preserves unchanged image nodes and ordering',()=>{
 const grid={children:[],insertBefore(node,before){const index=this.children.indexOf(node);if(index>=0)this.children.splice(index,1);const target=before?this.children.indexOf(before):this.children.length;this.children.splice(target,0,node);}};
 let rendered=0;
 const render=group=>{rendered++;const node={dataset:{group:String(group.id)},remove(){grid.children.splice(grid.children.indexOf(node),1);}};return node;};
 syncGroupCards(grid,[{id:1,count:1},{id:2,count:1}],render);
 const [first,second]=grid.children;
 syncGroupCards(grid,[{id:1,count:1},{id:2,count:2},{id:3,count:1}],render);
 assert.equal(grid.children[0],first);assert.notEqual(grid.children[1],second);assert.equal(rendered,4);
 const third=grid.children[2];syncGroupCards(grid,[{id:3,count:1},{id:1,count:1}],render);
 assert.deepEqual(grid.children,[third,first]);assert.equal(rendered,4);
});
test('100 groups per page, bounds and empty/shrinking collections',()=>{
 const groups=Array.from({length:405},(_,id)=>({id}));
 assert.equal(groupPage(groups,1).items.length,100);
 assert.equal(groupPage(groups,2).items[0].id,100);
 assert.equal(groupPage(groups,5).items.length,5);
 assert.equal(groupPage(groups,99).page,5);
 assert.equal(groupPage(groups,-2).page,1);
 assert.equal(groupPage(groups,NaN).page,1);
 assert.equal(groupPage(groups.slice(0,100),3).page,1);
 assert.deepEqual(groupPage([],3),{page:1,pages:1,total:0,start:0,items:[]});
});

test('Group photos uses the same 100-item page size',()=>{
 const faces=Array.from({length:405},(_,id)=>({id}));
 assert.equal(groupPage(faces,1).items.length,100);
 assert.equal(groupPage(faces,2).items[0].id,100);
 assert.equal(groupPage(faces,5).items.length,5);
 assert.equal(groupPage(faces,99).page,5);
});
