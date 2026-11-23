import test from 'node:test';
import assert from 'node:assert/strict';
import {renderIssues,importIssues,appendImportIssues} from '../web/issues.mjs';
function fixture(){
  const document={createElement(tag){return {tag,ownerDocument:document,children:[],events:{},dataset:{},open:false,
    replaceChildren(){this.children=[];},
    append(child){this.children.push(child);},
    addEventListener(name,callback){this.events[name]=callback;}};}};
  return {ownerDocument:document,children:[],append(child){this.children.push(child);},replaceChildren(){this.children=[];}};
}

test('imports have separate collapsed sections and errors open their own batch',()=>{
  const container=fixture();
  const batch={job_id:4,source_root:'F:\\PHOTOS\\PHOTO2020-2023',started_at:'2026-10-10T12:00:00Z',state:'running',unsupported_count:2,unsupported:[{extension:'.xyz',count:2,files:[]}],errors:[]};
  renderIssues(container,{imports:[batch,{...batch,job_id:3,source_root:'F:\\PHOTOS\\PHOTO2016-2019',errors:[{source_path:'bad.jpg',error:'Read failed'}]}]});
  assert.equal(container.children.length,2);
  assert.equal(container.children[0].open,false);
  assert.match(container.children[0].children[0].textContent,/PHOTO2020-2023 \(Import #4\) - 2 unsupported files/);
  assert.equal(container.children[1].open,true);
  assert.match(container.children[1].children[0].textContent,/PHOTO2016-2019.*1 failed files/);
  renderIssues(container,{imports:[]});
  assert.equal(container.children.length,0);
});

test('expanded unsupported groups restore all paths only for the correct import',()=>{
 const files=Array.from({length:251},(_,i)=>({source_path:`<album>/file-${i}.xyz`}));
 const data={imports:[{job_id:4,errors:[],unsupported:[{extension:'.xyz',count:files.length,files}]}]};
 const opened=new Set([JSON.stringify([4,'.xyz'])]);
 const container=fixture();appendImportIssues(container,importIssues(data,4),4,opened);
 const extension=container.children[0].children[1].children[0];
 assert.equal(extension.open,true);assert.equal(extension.children.length,252);
 assert.equal(extension.children[1].children[0].textContent,'<album>/file-0.xyz');
 extension.events.toggle();assert.equal(extension.children.length,252);
 const empty=fixture();appendImportIssues(empty,importIssues(data,5),5,opened);
 assert.equal(empty.children.length,0);
});
test('unsupported groups start collapsed, expand every file, and do not duplicate on reopen',()=>{
  const container=fixture();
  const files=Array.from({length:251},(_,i)=>({source_path:`file-${i}.xyz`}));
  renderIssues(container,{unsupported:[{extension:'.xyz',count:251,files}],errors:[]});
  assert.equal(container.children[0].textContent,'Unsupported files');
  const group=container.children[1];
  assert.equal(group.tag,'details');assert.equal(group.open,false);
  assert.equal(group.children.length,1);assert.equal(group.children[0].textContent,'.xyz (251)');
  group.open=true;group.events.toggle();assert.equal(group.children.length,252);
  group.open=false;group.events.toggle();group.open=true;group.events.toggle();
  assert.equal(group.children.length,252);
});
test('copy errors stay visible and filenames are text, not HTML',()=>{
  const container=fixture();
  renderIssues(container,{unsupported:[],errors:[{source_path:'<img>.jpg',error:'Checksum mismatch'}]});
  assert.equal(container.children[0].textContent,'Failed files (1)');
  assert.equal(container.children[1].children[0].textContent,'<img>.jpg');
  assert.equal(container.children[1].children[1].textContent,'Checksum mismatch');
  renderIssues(container,{unsupported:[],errors:[]});
  assert.equal(container.children.length,0);
});

test('empty import groups disappear while errors remain without zero unsupported labels',()=>{
 const container=fixture();
 const batch={job_id:1,source_root:'photos',started_at:'2026-10-18',state:'completed-review-required',unsupported_count:0,unsupported:[],errors:[]};
 renderIssues(container,{imports:[batch]});
 assert.equal(container.children.length,0);
 renderIssues(container,{imports:[{...batch,errors:[{source_path:'bad.jpg',error:'Read failed'}]}]});
 assert.match(container.children[0].children[0].textContent,/1 failed files/);
 assert.doesNotMatch(container.children[0].children[0].textContent,/unsupported/);
 const errors=fixture();appendImportIssues(errors,{...batch,errors:[{source_path:'bad.jpg'}]},1);
 assert.equal(errors.children.length,1);
 assert.equal(errors.children[0].className,'import-errors');
});
