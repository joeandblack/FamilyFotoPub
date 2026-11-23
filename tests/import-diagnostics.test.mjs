import test from 'node:test';
import assert from 'node:assert/strict';
import {setupImportDiagnostics} from '../web/import-diagnostics.mjs';

test('People warnings show filenames and reasons without duplicating status entries',async()=>{
 const previous=globalThis.document;
 const node=tag=>({tag,children:[],textContent:'',append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(){},addEventListener(){},showModal(){this.open=true;}});
 const body=node('body');globalThis.document={body,createElement:node};
 const text=n=>[n.textContent,...n.children.map(text)].join(' ');
 try{
  const show=setupImportDiagnostics(async()=>[
   {stage:'Discover people',severity:'warning',media_id:4796,name:'portrait.tif',error:'corrupt_or_unsafe_media',detail:'The embedded color profile could not be converted to sRGB for face detection.'},
   {stage:'Discover people',severity:'warning',media_id:12,name:'animation.gif',error:'unsupported_media'},
  ]);
  await show({id:1},'warning',{errors:[{media_id:4796,error:'corrupt_or_unsafe_media'},{media_id:12,error:'unsupported_media'},{media_id:13,error:'unsupported_media',detail:'Image format GIF is not supported for face detection'}]},'Discover people');
  assert.match(text(body),/portrait.tif \(Media #4796\)/);
  assert.match(text(body),/embedded color profile could not be converted to sRGB/);
  assert.match(text(body),/animation.gif \(Media #12\)/);
  assert.match(text(body),/exact reason was not saved by the earlier scan/);
  assert.match(text(body),/original files are retained/);
  assert.doesNotMatch(text(body),/corrupt_or_unsafe_media|unsupported_media/);
  const summaries=body.children[0].children[2].children.filter(n=>n.tag==='details').map(n=>n.children[0].textContent);
  assert.equal(summaries.length,2);
  assert.match(summaries[0],/\(1\)$/);
  assert.match(summaries[1],/\(2\)$/);
 }finally{globalThis.document=previous;}
});

test('stage links isolate warnings while overview retains every stage',async()=>{
 const previous=globalThis.document;
 const node=()=>({children:[],textContent:'',append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(){},addEventListener(){},showModal(){this.open=true;}});
 const body=node();globalThis.document={body,createElement:node};
 try{
  const show=setupImportDiagnostics(async()=>[
   {stage:'Photo duplicate scan',severity:'warning',name:'photo.jpg',error:'photo issue'},
   {stage:'Video processing',severity:'warning',name:'clip.avi',error:'video issue'},
   {stage:'Video conversion',severity:'warning',name:'clip2.avi',error:'conversion issue'}]);
  const text=n=>[n.textContent,...n.children.map(text)].join(' ');
  await show({id:2},'warning',null,'Photo duplicate scan');
  assert.match(text(body),/photo issue/);assert.doesNotMatch(text(body),/video issue|conversion issue/);
  assert.match(text(body),/Scan unexamined photos/);
  await show({id:2},'warning',null,'Video processing');
  assert.match(text(body),/video issue/);assert.doesNotMatch(text(body),/photo issue|conversion issue|Scan unexamined photos/);
  await show({id:2},'warning');
  for(const issue of ['photo issue','video issue','conversion issue'])assert.ok(text(body).includes(issue));
 }finally{globalThis.document=previous;}
});

test('truncated photo warnings share one group without losing files or changing diagnostics',async()=>{
 const previous=globalThis.document;
 const node=tag=>({tag,children:[],textContent:'',append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},setAttribute(){},addEventListener(){},showModal(){this.open=true;}});
 const body=node('body');globalThis.document={body,createElement:node};
 const rows=[
  ...['image file is truncated (19 bytes not processed)','image file is truncated (102 bytes not processed)','image file is truncated'].map((error,i)=>({stage:'Photo duplicate scan',severity:'warning',name:`photo${i}.jpg`,error})),
  {stage:'Photo duplicate scan',severity:'warning',name:'large.jpg',error:'Image exceeds manual review limit of 60 megapixels'},
  {stage:'Photo duplicate scan',severity:'warning',name:'transparent.png',error:'Transparent images require a separate comparison method'},
  {stage:'Video processing',severity:'warning',name:'clip.avi',error:'image file is truncated'},
 ];
 const original=JSON.stringify(rows);
 try{
  const show=setupImportDiagnostics(async()=>rows);
  await show({id:15},'warning');
  const groups=body.children[0].children[2].children.filter(n=>n.tag==='details');
  assert.equal(groups.length,4);
  assert.equal(groups[0].children[0].textContent,'Photo duplicate scan: Image file is truncated (3)');
  assert.deepEqual(groups[0].children[1].children.map(n=>n.textContent),['photo0.jpg','photo1.jpg','photo2.jpg']);
  assert.equal(JSON.stringify(rows),original);
 }finally{globalThis.document=previous;}
});

test('large-image actions call scoped endpoints and skip requires confirmation',async()=>{
 const previous=globalThis.document;
 const node=tag=>({tag,children:[],textContent:'',append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},remove(){this.removed=true;},focus(){},close(){this.open=false;},setAttribute(){},addEventListener(){},showModal(){this.open=true;}});
 const body=node('body');globalThis.document={body,createElement:node};
 const calls=[];
 let failed=false;
 try{
  const show=setupImportDiagnostics(async(url,options)=>{
   if(options){calls.push(url);return {message:'Accepted'};}
   return [{stage:'Photo duplicate scan',severity:'warning',name:'large.jpg',error:failed?'Large image scan failed: Insufficient memory':'Image exceeds manual review limit of 60 megapixels'}];
  });
  await show({id:2},'warning');
  let content=body.children[0].children[2],actions=content.children.find(n=>n.tag==='div');
  assert.equal(actions.children.filter(n=>n.tag==='button').length,1);
  await actions.children[0].onclick();
  assert.deepEqual(calls,['/api/import/2/photo-duplicates/large-images']);
  assert.equal(actions.children.filter(n=>n.tag==='button').length,1);
  failed=true;await show({id:2},'warning');
  content=body.children[0].children[2];actions=content.children.find(n=>n.tag==='div');
  assert.equal(actions.children.filter(n=>n.tag==='button').length,2);
  let pending=actions.children[1].onclick();
  body.children.at(-1).children.at(-1).children[0].onclick();
  await pending;assert.equal(calls.length,1);
  pending=actions.children[1].onclick();
  body.children.at(-1).children.at(-1).children[1].onclick();
  await pending;
  assert.equal(calls[1],'/api/import/2/photo-duplicates/large-images/skip');
  assert.ok(content.children.find(n=>n.tag==='details').removed);
 }finally{globalThis.document=previous;}
});
