import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,existsSync} from 'node:fs';
import {resolve,dirname} from 'node:path';

test('People entry point resolves its complete local module graph, including side-effect imports',()=>{
 const seen=new Set();
 function visit(file){
  if(seen.has(file))return;
  seen.add(file);
  assert.ok(existsSync(file),`Missing browser module: ${file}`);
  const source=readFileSync(file,'utf8');
  for(const match of source.matchAll(/(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)["']([^"']+)["']/g)){
   if(match[1].startsWith('.'))visit(resolve(dirname(file),match[1].split('?')[0]));
  }
 }
 visit(resolve('face_detect/web/automatic-people-preview.mjs'));
 assert.ok(seen.has(resolve('web/session-activity.mjs')));
});
