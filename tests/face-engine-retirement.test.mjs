import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,existsSync,readdirSync} from 'node:fs';
import {resolve,dirname} from 'node:path';

test('browser modules do not depend on removed pilot modules',()=>{
 const root=resolve('web');
 for(const file of readdirSync(root).filter(f=>/\.(mjs|js)$/.test(f))){
  const path=resolve(root,file),source=readFileSync(path,'utf8');
  for(const match of source.matchAll(/\bfrom\s+['"](\.\/[^'"]+)['"]/g)){
   assert(existsSync(resolve(dirname(path),match[1])),`${file}: ${match[1]}`);
  }
 }
 const app=readFileSync(resolve(root,'app.js'),'utf8');
 assert(!app.includes('setupFaces'));
 assert(!app.includes('setupPeople('));
 assert(app.includes("if(['people','faces'].includes(view))view=previewNav.hidden?'all':'people-preview'"));
 assert(!readFileSync(resolve(root,'index.html'),'utf8').includes('/web/people.css'));
});
