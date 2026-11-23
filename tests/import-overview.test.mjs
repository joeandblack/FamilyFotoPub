import test from 'node:test';
import assert from 'node:assert/strict';
import {overviewRow,overviewStage,renderImportOverview,sortImports,runtime,overviewColumns,startTime} from '../web/import-overview.mjs';
import {normalizeNavigation} from '../web/navigation.mjs';
const batch={id:21,source_root:'F:\\PHOTOS\\PHOTO2025',state:'completed-review-required',import_mode:'missing-only',total:56,total_bytes:1000000000,counts:{imported:43,skipped_duplicate:9,duplicate:0,pending:4},stages:{metadata:{done:2,pending:1},conversion:{done:2},duplicates:{pending:43}}};
batch.media_totals={photo:{files:40,bytes:1000000000},video:{files:7,bytes:2000000000},unclassified_duplicates:{files:9,bytes:100}};
test('overview contains requested columns and includes skipped duplicates',()=>{
 const row=overviewRow(batch,{}, {21:{state:'completed',done:143,total:143}});
 assert.equal(row.length,17);assert.equal(row[1],'PHOTO2025');assert.equal(row[2],'--');assert.ok(!overviewColumns.some(([title])=>title==='Mode'));
 assert.equal(row[8],'43');assert.equal(row[9],'9');assert.match(row[5],/GB/);
 assert.deepEqual(row.slice(4,8),['40 *','1.0 GB *','7 *','2.0 GB *']);
 assert.equal(row[13],'Ready to resume');assert.equal(row[14],'Complete');assert.equal(row[16],'Complete');
});
test('active, paused, cancelled, not applicable and unavailable states are distinct',()=>{
 assert.equal(overviewStage(batch,'metadata',{busy:true,job_id:21,stage:'metadata'}),'Running');
 assert.equal(overviewStage({...batch,metadata_status:{state:'paused'}},'metadata',{}),'Paused');
 assert.equal(overviewStage({...batch,state:'cancelled'},'conversion',{}),'Cancelled');
 assert.equal(overviewStage({...batch,stages:{}},'conversion',{}),'Not applicable');
 assert.equal(overviewRow(batch,{},null)[16],'Unavailable');
 assert.equal(overviewRow(batch,{},{} )[16],'Not started');
});
test('overview survives browser refresh in navigation state',()=>{
 assert.equal(normalizeNavigation({view:'import-overview'}).view,'import-overview');
});
test('table renders all columns and treats import names as plain text',()=>{
 const doc={createElement:tag=>({tag,ownerDocument:doc,dataset:{},children:[],append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=nodes;}})};
 const table=doc.createElement('table');
 renderImportOverview(table,[{...batch,source_label:'<img onerror=alert(1)>'}],{},{});
 assert.equal(table.children[0].children[0].children.length,17);
 const cells=table.children[1].children[0].children;
 assert.equal(cells.length,17);assert.equal(cells[1].textContent,'<img onerror=alert(1)>');assert.equal(cells[1].innerHTML,undefined);
 assert.equal(cells[14].title,'Pending: AVI: 0 · FLV: 0 · MTS: 0');
 assert.match(table.children[2].textContent,/types were not recorded/);
 renderImportOverview(table,[],{},{});assert.equal(table.children[1].children[0].children[0].colSpan,17);
});
test('all six sort keys use raw values and both directions',()=>{
 const rows=[{...batch,id:2,source_label:'Album 2',total:2,total_bytes:900,counts:{imported:2,skipped_duplicate:2}},
 {...batch,id:10,source_label:'Album 10',total:10,total_bytes:1100,counts:{imported:10,duplicate:10}}];
 rows[0].media_totals={photo:{files:2,bytes:900},video:{files:2,bytes:900}};
 rows[1].media_totals={photo:{files:10,bytes:1100},video:{files:10,bytes:1100}};
 for(const key of ['id','name','photo_files','photo_bytes','video_files','video_bytes','copied','dups']){
  assert.deepEqual(sortImports(rows,key,'asc').map(r=>r.id),[2,10]);assert.deepEqual(sortImports(rows,key,'desc').map(r=>r.id),[10,2]);
 }
 assert.equal(rows[0].id,2);
});
test('runtime and issue counts distinguish unknown timings and numeric counts',()=>{
 assert.equal(runtime(null),'--');assert.equal(runtime(0),'0m');assert.equal(runtime(7380),'2h 3m');
 const row=overviewRow({...batch,import_status:{active_seconds:7380},counts:{unsupported:3,error:2},stages:{metadata:{warning:4,error:1}}},{},{21:{errors:[{}],run_seconds:60}});
 assert.equal(row[3],'Complete 2h 3m');assert.deepEqual(row.slice(10,13),['3','3','5']);assert.ok(row[16].endsWith(' 1m'));
 assert.equal(overviewColumns.filter(c=>c[1]).length,9);
});
test('import runtime sorts seconds in either direction with unknown values last',()=>{
 const rows=[{id:1},{id:2,import_status:{active_seconds:7200}},{id:3,import_status:{active_seconds:60}},{id:4,import_status:{active_seconds:0}},{id:5,import_status:{active_seconds:null}}];
 assert.deepEqual(sortImports(rows,'import_runtime','asc').map(r=>r.id),[4,3,2,1,5]);
 assert.deepEqual(sortImports(rows,'import_runtime','desc').map(r=>r.id),[2,3,4,1,5]);
});

test('nonzero issue counters open the matching import and survive sorting',()=>{
 const doc={createElement:tag=>({tag,ownerDocument:doc,dataset:{},children:[],append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=nodes;}})};
 const table=doc.createElement('table'),calls=[];
 const b={...batch,counts:{error:2},stages:{duplicates:{warning:677}}};
 const review=(...args)=>calls.push(args);
 renderImportOverview(table,[b],{},{},review);
 let cells=table.children[1].children[0].children;
 cells[11].children[0].onclick();cells[12].children[0].onclick();
 assert.deepEqual(calls.map(c=>[c[0].id,c[1]]),[[b.id,'error'],[b.id,'warning']]);
 table.children[0].children[0].children[0].children[0].onclick();
 table.children[1].children[0].children[12].children[0].onclick();assert.equal(calls.length,3);
 renderImportOverview(table,[{...b,counts:{},stages:{}}],{},{},review);
 cells=table.children[1].children[0].children;assert.equal(cells[11].children.length,0);assert.equal(cells[12].children.length,0);
});

test('operations combine status and compact runtime without extra columns',()=>{
 const b={...batch,metadata_status:{run_seconds:16260},duplicates_status:{run_seconds:1020},stages:{metadata:{done:2},duplicates:{pending:2}}};
 const row=overviewRow(b,{busy:true,job_id:21,stage:'duplicates'},{});
 assert.equal(row[13],'Complete 4h 31m');assert.equal(row[15],'Running 17m');
 assert.equal(overviewColumns.some(([title])=>title.toLowerCase().includes('runtime')),false);
 assert.deepEqual(overviewColumns[3],['Import','import_runtime']);
});

test('start time follows Name and uses local date and time with missing fallback',()=>{
 const timestamp='2026-01-03T08:05:00';
 assert.equal(overviewColumns[2][0],'Start time');
 assert.equal(overviewRow({...batch,started_at:timestamp},{},{})[2],'2026-01-03 08:05');
 assert.equal(startTime('2026-10-23T20:09:00'),'2026-10-23 20:09');
 for(const value of [undefined,null,'','invalid'])assert.equal(startTime(value),'--');
});

test('start time is display-only while ID remains sortable',()=>{
 assert.deepEqual(overviewColumns[2],['Start time']);
 assert.deepEqual(overviewColumns[0],['ID','id']);
});

test('conversion includes pending counts without a separate sortable column',()=>{
 const rows=[{...batch,id:2,conversion_pending:315},{...batch,id:10,conversion_pending:15},{...batch,id:19,conversion_pending:20}];
 assert.deepEqual(overviewColumns[14],['Conversion']);
 assert(!overviewColumns.some(([title,key])=>title==='Conversion pending'||key==='conversion_pending'));
 assert.equal(overviewRow(rows[1],{},{} )[14],'Complete · 15 pending');
 assert.equal(overviewRow(batch,{},{} )[14],'Complete');
});
