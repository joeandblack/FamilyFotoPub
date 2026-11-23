import test from 'node:test';
import assert from 'node:assert/strict';
import {viewForRole,readNavigation,navigationKey} from '../web/navigation.mjs';

for(const role of ['user','guest',undefined]){
 test(`admin pages are unavailable for ${role??'unresolved role'}`,()=>{
  for(const view of ['imports','import-overview','settings','storage'])assert.equal(viewForRole(view,role),'all');
  for(const view of ['all','albums','image','video','uploads','people-preview','trash'])assert.equal(viewForRole(view,role),view);
 });
}
test('admin navigation remains unchanged',()=>{
 for(const view of ['imports','import-overview','settings'])assert.equal(viewForRole(view,'admin'),view);
});
test('restored admin navigation falls back to Library for a regular user',()=>{
 const saved=readNavigation({getItem:key=>{assert.equal(key,navigationKey);return JSON.stringify({view:'settings'});}});
 assert.equal(viewForRole(saved.view,'user'),'all');
});
