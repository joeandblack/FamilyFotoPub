import test from 'node:test';
import assert from 'node:assert/strict';
import {renderImportHistory} from '../web/import-history.mjs';
test('history displays counts, safe filenames, scanning and cancelled states',()=>{
  const document={createElement(){return {ownerDocument:document,children:[],append(...items){this.children.push(...items);},replaceChildren(){this.children=[];}};}};
  const container=document.createElement('div');
  const batch={id:1,source_root:'F:\\PHOTOS\\<album>',started_at:'2026-10-13T00:00:00Z',state:'running',total:3,total_bytes:3000,counts:{imported:1,duplicate:1,pending:1},bytes_by_state:{imported:1000,duplicate:1000},inventory_complete:false};
  renderImportHistory(container,[batch,{...batch,id:2,state:'cancelled'}]);
  assert.match(container.children[0].children[0].textContent,/<album>/);
  assert.match(container.children[0].children[2].textContent,/3 files scanned so far.*Copied 1.*Duplicates 1.*Pending 1/);
  assert.equal(container.children[1].children[2].textContent,'Cancelled import');
  renderImportHistory(container,[]);assert.equal(container.textContent,'No imports recorded.');
});
