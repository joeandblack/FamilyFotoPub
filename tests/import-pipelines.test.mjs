import test from 'node:test';
import assert from 'node:assert/strict';
import {stageProgress,renderPipelines,importSummary,peoplePercent} from '../web/import-pipelines.mjs';
test('People percentage includes excluded photos and clamps completion',()=>{
 assert.equal(peoplePercent({done:20,excluded:5,total:100}),'25.0%');
 assert.equal(peoplePercent({done:101,total:100}),'100.0%');
 assert.equal(peoplePercent({done:0,total:0}),'');
 assert.match(importSummary(batch(1),{},{state:'running',done:25,total:100}).text,/Running Discover people · 25.0%/);
});
import {overviewStage} from '../web/import-overview.mjs';
test('Imports stages and overview share status precedence',()=>{
 for(const stage of ['import','metadata','conversion','duplicates'])for(const state of ['paused','running','paused-error','waiting-service'])for(const pending of [0,1]){
  const item=batch(4),container=fixture();
  item[stage+'_status']={state,run_seconds:60};
  if(stage!=='import')item.stages[stage]={done:2,pending};
  renderPipelines(container,[item],{}, {command(){},options(){}});
  const row=rows(container.children[0])[['import','metadata','conversion','duplicates'].indexOf(stage)];
  assert.equal(row.children[0].children[1].textContent.split(' · ')[0],overviewStage(item,stage,{}));
 }
});
test('collapsed history names active post-processing instead of Imported',()=>{
 const item=batch(22);
 assert.deepEqual(importSummary(item,{},null),{text:'Imported',running:false});
 assert.deepEqual(importSummary(item,{}, {state:'running'}),{text:'Running Discover people',running:true});
 assert.match(importSummary(item,{busy:true,job_id:22,stage:'conversion'}, {state:'waiting-for-foreground'}).text,/Converting videos.*Discover people waiting/);
 assert.equal(importSummary(item,{busy:true,job_id:21,stage:'conversion'},null).text,'Imported');
 assert.equal(importSummary(item,{}, {state:'waiting-service'}).running,false);
 assert.match(importSummary(item,{}, {state:'blocked'}).text,/stopped with error/);
});
test('each stage uses only its own group counts',()=>{
 const batch={counts:{imported:3,duplicate:2,unsupported:1},stages:{metadata:{done:2,pending:2},conversion:{done:1,pending:3}}};
 assert.equal(stageProgress(batch,'import').percent,100);
 assert.equal(stageProgress(batch,'metadata').percent,50);
 assert.equal(stageProgress(batch,'conversion').percent,25);
 assert.equal(stageProgress({stages:{}},'metadata').percent,0);
});

test('completed conversion ignores stale paused status in summary and runtime',()=>{
 const container=fixture(),item=batch(4);
 item.stages.metadata={done:1069,warning:7};item.stages.conversion={done:7};
 item.conversion_status={state:'paused',run_seconds:5220};
 assert.equal(importSummary(item,{},null).text,'Imported');
 renderPipelines(container,[item],{}, {command(){},options(){}});
 const conversion=rows(container.children[0])[2];
 const runtime=conversion.children.find(e=>e.textContent?.startsWith('Run '));
 assert.equal(runtime.textContent,'Run 1hr 27m · Complete');
 item.stages.conversion.pending=1;
 assert.match(importSummary(item,{},null).text,/paused/);
 renderPipelines(container,[item],{}, {command(){},options(){}});
 assert.match(rows(container.children[0])[2].children.find(e=>e.textContent?.startsWith('Run ')).textContent,/Paused/);
});

test('photo scan is manual and selected import can pause it',()=>{
 const container=fixture(),item=batch(1),commands=[];
 item.stages.duplicates={done:1,pending:1};
 renderPipelines(container,[item],{busy:true,job_id:1,stage:'duplicates',duplicate_busy:true},{command:(...args)=>commands.push(args),options(){}});
 const scan=rows(container.children[0])[3];
 assert.equal(Boolean(button(scan).disabled),false);button(scan).onclick();
 assert.deepEqual(commands[0].slice(0,2),[1,'pause']);
 assert.equal(stageProgress(item,'duplicates').percent,50);
});

function fixture(){
 const document={
  createElement(tag){return {tag,ownerDocument:document,children:[],dataset:{},
   append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},
   setAttribute(){},addEventListener(){},querySelectorAll(){return this.children.filter(e=>e.tag==='details'&&e.open);}};},
  createTextNode(text){return {textContent:text};}
 };
 return document.createElement('div');
}
function batch(id){return {id,source_root:`F:\\PHOTOS\\group${id}`,started_at:'2026-10-13T00:00:00Z',
 state:'completed-review-required',total:2,total_bytes:100,counts:{imported:2},
 stages:{metadata:{done:1,pending:1},conversion:{pending:2}}};}
const rows=group=>group.children.filter(e=>e.className==='pipeline-stage');
const button=row=>row.children[0].children.find(e=>e.tag==='button');

test('fast conversion labels playback review and shows the current phase',()=>{
 const container=fixture(),item=batch(1),reviewed=[];
 item.conversion_status={state:'running',current_step:'Publishing MP4',execution_policy:{retained_original_validation:'quick'}};
 renderPipelines(container,[item],{busy:true,job_id:1,stage:'conversion'},{command(){},options(){},reviewPlayback:batch=>reviewed.push(batch.id)});
 const text=rows(container.children[0])[2].children.map(e=>e.textContent||'').join(' ');
 assert.match(text,/Originals retained/);assert.match(text,/Publishing MP4/);
 rows(container.children[0])[2].children.find(e=>e.textContent==='Review playback').onclick();
 assert.deepEqual(reviewed,[1]);
});

test('progress bars only occupy space for partial progress including paused stages',()=>{
 const container=fixture(),item=batch(1);
 item.stages.duplicates={done:2};item.metadata_status={state:'paused'};
 renderPipelines(container,[item],{}, {command(){},options(){},peopleReview:{status:{1:{state:'paused',done:2,excluded:1,total:5}},command(){},review(){}}});
 assert.deepEqual(rows(container.children[0]).map(row=>row.children.filter(e=>e.tag==='progress').length),[0,1,0,0,1]);
 for(const done of [0,5]){
  renderPipelines(container,[item],{}, {command(){},options(){},peopleReview:{status:{1:{done,total:5}},command(){},review(){}}});
  assert.equal(rows(container.children[0]).at(-1).children.some(e=>e.tag==='progress'),false);
 }
});

test('completed downstream stages hide their automatic checkboxes and empty wrappers',()=>{
 const container=fixture(),item=batch(1);
 item.stages={metadata:{done:2},conversion:{done:1,warning:1},duplicates:{done:2}};
 const callbacks={command(){},options(){}};
 renderPipelines(container,[item],{},callbacks);
 assert(rows(container.children[0]).every(row=>!row.children.some(e=>e.className==='pipeline-options')));
 item.stages.duplicates.pending=1;
 renderPipelines(container,[item],{},callbacks);
 assert(rows(container.children[0]).slice(0,3).every(row=>!row.children.some(e=>e.className==='pipeline-options')));
 assert.equal(Boolean(button(rows(container.children[0])[3]).disabled),false);
});

test('not-applicable conversion has no automatic checkbox on a pending metadata stage',()=>{
 const container=fixture(),item=batch(21);item.stages.conversion={};item.stages.duplicates={pending:2};
 renderPipelines(container,[item],{}, {command(){},options(){}});
 const stages=rows(container.children[0]);
 const switches=stages[1].children.find(e=>e.className==='pipeline-options');
 assert.deepEqual(switches.children.map(label=>label.children[0].dataset.option),['auto_duplicates']);
 assert(!stages[2].children.some(e=>e.className==='pipeline-options'));
});

test('pipeline warning and error links open diagnostics for their import',()=>{
 const container=fixture(),item=batch(23),calls=[];
 item.counts.error=2;item.stages.duplicates={warning:677};
 const person={state:'blocked',errors:[{media_id:1,error:'test'}]};
 renderPipelines(container,[item],{}, {command(){},options(){},reviewIssues:(...args)=>calls.push(args),peopleReview:{status:{23:person},command(){},review(){}}});
 const links=rows(container.children[0]).flatMap(r=>r.children.filter(e=>e.className==='pipeline-counts').flatMap(c=>c.children.filter(e=>e.tag==='button')));
 assert.deepEqual(links.map(l=>l.textContent),['Errors 2','Warnings 677','Warnings 1','Errors 1']);
 for(const link of links)link.onclick();
 assert.deepEqual(calls.map(c=>c[3]),['Import','Photo duplicate scan','Discover people','Discover people']);
 assert.deepEqual(calls.map(c=>[c[0].id,c[1],c[2]===person]),[[23,'error',true],[23,'warning',true],[23,'warning',true],[23,'error',true]]);
});

test('People start is disabled while active with separate pause control',()=>{
 for(const state of ['running','queued','waiting-for-foreground']){
  const container=fixture();
  renderPipelines(container,[batch(14)],{busy:false},{command(){},options(){},peopleReview:{status:{14:{state,done:0,total:260}},command(){},review(){}}});
  const row=rows(container.children[0]).at(-1);
  const buttons=row.children[0].children.filter(e=>e.tag==='button');
  assert.equal(buttons[0].disabled,true);
  assert.equal(buttons[1].disabled,false);
 }
});

test('Discover people belongs to each import, not only the pilot',()=>{
 const container=fixture(),calls=[];
 renderPipelines(container,[batch(5),batch(14)],{busy:false},{command(){},options(){},peopleReview:{status:{14:{state:'completed',done:260,total:260}},command:(...args)=>calls.push(args),review:id=>calls.push(id)}});
 const first=rows(container.children[0]).at(-1).children[0].children.filter(e=>e.tag==='button');
 assert.equal(first[0].disabled,false);first[0].onclick();first[2].onclick();
 assert.deepEqual(calls[0].slice(0,2),[5,'resume']);assert.equal(calls[1],5);
 const complete=rows(container.children[1]).at(-1).children[0].children.filter(e=>e.tag==='button');
 assert.equal(complete[0].disabled,true);
});

test('scanning is active only with a worker and otherwise shows interrupted',()=>{
 const container=fixture(),item=batch(18);item.state='scanning';item.counts={pending:2};
 renderPipelines(container,[item],{busy:false},{command(){},options(){}});
 assert.match(rows(container.children[0])[0].children[0].children[1].textContent,/Interrupted/);
 renderPipelines(container,[item],{busy:true,job_id:18,stage:'import'},{command(){},options(){}});
 assert.match(rows(container.children[0])[0].children[0].children[1].textContent,/Scanning/);
 assert.match(button(rows(container.children[0])[0]).innerHTML,/Pause/);
});

test('completed duplicate scan stays disabled with automatic trash enabled',()=>{
 const container=fixture(),item=batch(1);item.auto_trash_duplicates=true;
 item.stages.duplicates={done:100,warning:1};
 renderPipelines(container,[item],{busy:false},{command(){},options(){}});
 assert.equal(button(rows(container.children[0])[3]).disabled,true);
 item.stages.duplicates.pending=1;
 renderPipelines(container,[item],{busy:false},{command(){},options(){}});
 assert.equal(Boolean(button(rows(container.children[0])[3]).disabled),false);
});

test('only active import opens by default and completion collapses all groups',()=>{
 const container=fixture(),batches=[batch(2),batch(1)],callbacks={command(){},options(){}};
 renderPipelines(container,batches,{busy:true,job_id:1,stage:'duplicates'},callbacks);
 assert.deepEqual(container.children.map(e=>e.open),[false,true]);
 renderPipelines(container,batches,{busy:false,job_id:1,stage:'duplicates',state:'completed'},callbacks);
 assert.deepEqual(container.children.map(e=>e.open),[false,false]);
 container.children[0].open=true;
 renderPipelines(container,batches,{busy:false},callbacks);
 assert.deepEqual(container.children.map(e=>e.open),[true,false]);
 renderPipelines(container,batches,{busy:true,job_id:1,stage:'metadata'},callbacks);
 assert.deepEqual(container.children.map(e=>e.open),[false,true]);
 const idle=fixture();renderPipelines(idle,batches,{busy:false},callbacks);
 assert.deepEqual(idle.children.map(e=>e.open),[false,false]);
});

test('downstream options appear under stages and synchronize within their import',async()=>{
 const container=fixture(),changes=[],item=batch(1);item.stages.duplicates={pending:2};item.state='running';item.counts={pending:2};
 const second={...batch(2),state:'running',counts:{pending:2}};
 renderPipelines(container,[item,second],{busy:true,job_id:1,stage:'import'},{command(){},options:(...args)=>changes.push(args)});
 const options=rows(container.children[0]).map(row=>row.children.find(e=>e.className==='pipeline-options')?.children.map(label=>label.children[0])||[]);
 assert.deepEqual(options.map(inputs=>inputs.length),[3,2,1,0]);
 assert.equal(options[2][0].disabled,false);
 options[2][0].checked=true;await options[2][0].onchange();
 assert.ok([options[0][2],options[1][1],options[2][0]].every(input=>input.checked));
 options[1][0].checked=true;await options[1][0].onchange();
 assert.equal(options[0][0].checked,true);assert.equal(options[0][1].checked,true);
 assert.deepEqual(changes.at(-1).slice(0,2),[1,{auto_metadata:true,auto_conversion:true,auto_duplicates:true}]);
 const other=rows(container.children[1])[0].children.find(e=>e.className==='pipeline-options');
 assert.ok(other.children.every(label=>!label.children[0].checked));
});

test('output duplicates are terminal and displayed separately from warnings',()=>{
 const container=fixture(),item=batch(1);
 item.stages={metadata:{done:3},conversion:{done:1,warning:1,duplicate:1}};
 assert.equal(stageProgress(item,'conversion').percent,100);
 renderPipelines(container,[item],{busy:false},{command(){},options(){}});
 const conversion=rows(container.children[0])[2];
 assert.equal(button(conversion).disabled,true);
 assert.ok(conversion.children.some(row=>row.textContent?.includes('Warnings 1 - Output duplicates 1')));
});

test('only the active group can pause and polling preserves expanded groups',()=>{
 const container=fixture(),commands=[],batches=[batch(1),batch(2)];
 const callbacks={command:(...args)=>commands.push(args),options(){}};
 renderPipelines(container,batches,{busy:true,job_id:2,stage:'metadata'},callbacks);
 assert.ok(rows(container.children[0]).every(row=>button(row).disabled));
 const active=button(rows(container.children[1])[1]);
 assert.equal(!!active.disabled,false);active.onclick();
 assert.deepEqual(commands[0].slice(0,2),[2,'pause']);
 container.children[0].open=true;
 renderPipelines(container,batches,{busy:true,job_id:2,stage:'metadata',pause_requested:true},callbacks);
 assert.equal(container.children[0].open,true);
 assert.equal(button(rows(container.children[1])[1]).textContent,'Pausing...');
 assert.equal(button(rows(container.children[1])[1]).disabled,true);
});

test('stage prerequisites and automatic options belong to the selected import',()=>{
 const container=fixture(),changes=[],commands=[];
 renderPipelines(container,[batch(3)],{busy:false},{command:(...args)=>commands.push(args),options:(...args)=>changes.push(args)});
 const group=container.children[0],stages=rows(group);
 assert.equal(button(stages[0]).disabled,true);
 assert.equal(button(stages[1]).disabled,false);
 assert.equal(button(stages[2]).disabled,true);
 button(stages[1]).onclick();assert.deepEqual(commands[0].slice(0,2),[3,'metadata/resume']);
 assert(!stages[0].children.some(e=>e.className==='pipeline-options'));
 const inputs=stages[1].children.find(e=>e.className==='pipeline-options').children.map(label=>label.children[0]);
 assert.deepEqual(inputs.map(input=>input.checked),[false]);
 inputs[0].checked=true;inputs[0].onchange();
 assert.deepEqual(changes[0].slice(0,2),[3,{auto_metadata:true,auto_conversion:true,auto_duplicates:false}]);
});

test('unsupported files are last in their own import, with failures preserved and empty sections hidden',()=>{
 const container=fixture();
 const issues={imports:[{job_id:2,errors:[{source_path:'two/bad.jpg',error:'Copy failed'}],unsupported:[{extension:'.xyz',count:1,files:[{source_path:'two/file.xyz'}]}]}]};
 renderPipelines(container,[batch(1),batch(2),{...batch(3),state:'cancelled'}],{busy:false},{command(){},options(){},issues});
 assert.equal(container.children[0].children.some(e=>e.className==='import-unsupported'),false);
 const counts=container.children[0].children.flatMap(e=>e.children||[]).filter(e=>e.className==='pipeline-counts');
 assert.equal(counts.some(e=>e.textContent.includes('Unsupported')),false);
 const second=container.children[1];
 assert.equal(second.open,false);
 assert.equal(second.children.at(-2).className,'import-errors');
 assert.equal(second.children.at(-2).children[1].children[0].textContent,'two/bad.jpg');
 const extension=second.children.at(-1).children[1].children[0];
 assert.equal(extension.children[0].textContent,'.xyz (1)');
 assert.equal(extension.dataset.issueKey,JSON.stringify([2,'.xyz']));
 assert.equal(extension.open,false);
 assert.equal(container.children[2].children.some(e=>e.className==='import-unsupported'),false);
});

test('duplicate-only imports have no post-processing counts, controls or automatic actions',()=>{
 const container=fixture(),duplicate={...batch(4),counts:{duplicate:2},stages:{metadata:{},conversion:{}}};
 renderPipelines(container,[duplicate],{busy:false},{command(){throw Error('No duplicate action');},options(){throw Error('No duplicate options');}});
 assert.equal(stageProgress(duplicate,'metadata').total,0);assert.equal(stageProgress(duplicate,'conversion').total,0);
 const group=container.children[0];
 assert.ok(rows(group).every(row=>button(row).disabled));
 assert.ok(rows(group).flatMap(row=>row.children.filter(e=>e.className==='pipeline-options')).every(e=>e.children.every(label=>label.children[0].disabled)));
 assert.ok(!group.children.some(e=>e.textContent?.includes('Duplicates need no further processing')));
});

test('every import including cancelled partial imports opens its own Library filter',()=>{
 const container=fixture(),selected=[];
 renderPipelines(container,[batch(1),{...batch(2),state:'cancelled'}],{busy:false},{command(){},options(){},viewLibrary:id=>selected.push(id)});
 for(const group of container.children)group.children.find(e=>e.tag==='button'&&e.textContent==='View in Library').onclick();
 assert.deepEqual(selected,[1,2]);
});


test('face contention allows explicit handoff, then pending start can be cancelled',()=>{
 const container=fixture(),commands=[];
 const callbacks={command:(...args)=>commands.push(args),options(){}};
 renderPipelines(container,[batch(1),batch(2)],{busy:true,face_busy:true},callbacks);
 assert.equal(button(rows(container.children[0])[1]).disabled,false);
 button(rows(container.children[0])[1]).onclick();
 assert.deepEqual(commands[0].slice(0,2),[1,'metadata/resume']);
 renderPipelines(container,[batch(1),batch(2)],{busy:true,face_busy:true,handoff:{state:'pending',job_id:1,stage:'metadata'}},callbacks);
 const pending=button(rows(container.children[0])[1]);
 assert.equal(pending.disabled,false);assert.equal(pending.innerHTML,'Cancel pending start');
 pending.onclick();assert.deepEqual(commands[1].slice(0,2),[1,'pause']);
 assert.equal(button(rows(container.children[1])[1]).disabled,true);
 renderPipelines(container,[batch(1)],{busy:false,handoff:{state:'failed',job_id:1,stage:'metadata',error:'Expired; retry'}},callbacks);
 assert.ok(rows(container.children[0])[1].children.some(e=>e.textContent==='Expired; retry'));
});
