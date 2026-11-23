import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

for(const section of ['review','other'])test(`Select all and bulk actions in ${section}`,async()=>{
 const element=()=>({children:[],dataset:{},classList:{toggle(){}},replaceChildren(){this.children=[];},append(node){this.children.push(node);},setAttribute(){},removeAttribute(){},addEventListener(){}});
 const cards=[7,12,25].map(id=>({...element(),dataset:{group:String(id)}}));
 const grid={...element(),querySelectorAll:()=>cards};
 let toolbar,moved,matched,ignored,dragOptions;
 const context={document:{createElement:element},bindPersonMenu:(button,choices,choose)=>{button.onclick=()=>choose(choices()[0].person_id);return {close(){}};},setupDragSelection:(grid,options)=>{dragOptions=options;return {cancel(){}};},createPeopleActionProgress:status=>({start(text){status.textContent=text;},report(){},finish(text){status.textContent=text;},fail(error){status.textContent=error.message;}})};
 const source=readFileSync(new URL('../face_detect/web/people-review-selection.mjs',import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'').replace('export function','function');
 vm.createContext(context);vm.runInContext(source,context);
 context.setupReviewSelection(grid,{before(node){toolbar=node;}},section==='other'?null:async ids=>{moved=Array.from(ids);},{people:()=>[{person_id:9,name:'Test person'}],match:async(ids,pid)=>{matched=[Array.from(ids),pid];},ignore:async ids=>{ignored=Array.from(ids);}});
 assert.equal(dragOptions.plain,'add');
 const button=text=>toolbar.children.find(node=>node.textContent===text);
 const all=button('Select all');
 assert(!all.hidden);assert(!all.disabled);
 all.onclick();
 assert.equal(toolbar.children.at(-1).textContent,'3 selected');
 if(section==='review'){
  assert(!button('Move to Review later').disabled);
  await button('Move to Review later').onclick();
  assert.deepEqual(moved,[7,12,25]);
 }else assert(button('Move to Review later').hidden);
 assert(!all.hidden);
 all.onclick();
 const match=button('Move to person');
 assert(!match.disabled);
 await match.onclick();assert.deepEqual(matched,[[7,12,25],9]);
 all.onclick();assert(!button('Ignore').disabled);
 await button('Ignore').onclick();assert.deepEqual(ignored,[7,12,25]);
});
