import test from 'node:test';
import assert from 'node:assert/strict';
import {PAGE_SIZES,reviewImportFromURL} from '../web/page-size.mjs';
import {groupPage} from '../face_detect/web/people-group-pages.mjs';
test('page sizes retain defaults and slice without gaps',()=>{
 assert(PAGE_SIZES.includes(100)&&PAGE_SIZES.includes(1000));
 const groups=Array.from({length:250},(_,id)=>({id}));
 assert.equal(groupPage(groups,2,50).items[0].id,50);
 assert.equal(groupPage(groups,2,200).items.length,50);
 assert.equal(groupPage(groups,10,500).page,1);
});
test('import review links initialize a valid import scope',()=>{
 assert.equal(reviewImportFromURL('?desktop=1&import=22'),22);
 for(const search of ['', '?import=0','?import=-1','?import=no','?import=9007199254740992'])assert.equal(reviewImportFromURL(search),null);
});
