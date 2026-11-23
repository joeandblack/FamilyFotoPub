import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../face_detect/web/automatic-people-preview.mjs',import.meta.url),'utf8').replaceAll('\r\n','\n');
test('retired person search has no header button or dialog',()=>{
 const shell=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
 for(const text of [source,shell])assert(!text.includes('Find this person'));
 assert(!source.includes('/search-photos'));
 assert(!source.includes("dialog.id='person-search'"));
});
const begin=source.indexOf(" const controls=element('section'");
const end=source.indexOf(' const requestedImport=',begin);
function controls(integrated){
 const header={after(node){this.panel=node;}};
 const element=(tag,text,cls)=>({tag,text,cls,children:[],append(...nodes){this.children.push(...nodes);},setAttribute(key,value){this[key]=value;}});
 vm.runInNewContext(source.slice(begin,end),{integrated,desktopMode:true,element,document:{querySelector:()=>header}});
 return header.panel.children;
}
test('integrated People omits the entire import controls block and repeated heading',()=>{
 assert(source.includes("if(!integrated&&new URLSearchParams(location.search).has('jobs'))"));
 assert(source.includes("document.querySelector('header').hidden=true"));
 const shell=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
 assert(shell.includes('Run Discover people from each import on the Imports page.'));
 assert(shell.includes("document.querySelector('main > header').append(peopleToolbar)"));
 assert(shell.includes('&import=${id}#review-faces'));
 assert(source.includes('before:reviewSection,recheck'));
 assert(!source.includes('toolbar.insertBefore(recheck'));
 assert(source.includes("completedScan='completed-imports'"));
});
test('isolated fixture retains its own scan controls',()=>{
 const nodes=controls(false);
 for(const id of ['job-start','job-pause','job-resume','job-cancel','job-progress'])assert(nodes.some(n=>n.id===id));
});

test('People thumbnails are lazy and group loading opens before its request',()=>{
 assert(source.includes("tile.loading='lazy';tile.decoding='async'"));
 const detail=source.slice(source.indexOf('async function showDetail(gid,'),source.indexOf('let refreshTicket'));
 assert(detail.includes('async function showDetail('));
 assert(detail.indexOf('dialog.showModal()')<detail.indexOf('await request'));
 assert(detail.includes('ticket!==detailTicket||!dialog.open'));
 assert(source.includes("indicator.start(`${action==='ignore'?'Ignoring':'Moving'} ${ids.length} selected faces...`)"));
 assert(source.includes('await refreshAffectedGroups(group.id,personId,result.group_ids||[])'));
});

test('header actions reach existing handlers only from the same-origin parent',()=>{
 const clicked=[],messages=[],parent={postMessage:(...args)=>messages.push(args)};
 let listener;
 const start=source.indexOf("if(integrated){\n const ready=");
 const end=source.indexOf("if(!integrated&&new URLSearchParams",start);
 vm.runInNewContext(source.slice(start,end),{integrated:true,parent,location:{origin:'https://familyfoto.test'},
  refreshFromHeader:()=>clicked.push('refresh'),window:{addEventListener:(type,fn)=>{listener=fn;}},document:{getElementById:id=>({click:()=>clicked.push(id)})}});
 assert.equal(messages.length,1);
 const event={origin:'https://familyfoto.test',source:parent,data:{type:'familyfoto-people-action',action:'refresh'}};
 listener({...event,origin:'https://other.test'});listener({...event,source:{}});
 listener({...event,data:{type:'familyfoto-people-action',action:'job-start'}});
 listener({...event,data:{...event.data,action:'find-person'}});
 assert.deepEqual(clicked,[]);
 for(const action of ['refresh','review-faces','ignored-faces'])listener({...event,data:{...event.data,action}});
 assert.deepEqual(clicked,['refresh','review-faces','ignored-faces']);
});

test('successful refresh clears stale Detect again text but preserves active and failed states',async()=>{
 const start=source.indexOf('let refreshTicket=0');
 const end=source.indexOf("const reviewPage=element('section')",start);
 for(const [busy,failed] of [[false,false],[true,false],[false,true]]){
  const messages=[];
  const node={setAttribute(){},replaceChildren(){},open:false};
  const context={integrated:false,desktopMode:false,recheckBusy:busy,state:null,location:{hash:''},
   document:{querySelector:()=>node},reviewGroups:node,reviewHeading:{},reviewSection:{},
   reviewPages:{update(){}},otherPages:{update(){}},reviewSelection:{refresh(){}},otherSelection:{refresh(){}},refreshIgnored:async()=>{},suggestionsPanel:null,peopleSection:()=>'',card:()=>{},syncGroupCards(){},
   request:async()=>{if(failed)throw Error('Refresh failed');return {groups:[],outlier_faces:0};},
   renderMatchedPeople(){},recheckMessage:text=>messages.push(text)};
  const refreshCode=source.slice(start,end);
  vm.createContext(context);vm.runInContext(refreshCode,context);
  await context.refresh();
  assert.deepEqual(messages,!busy&&!failed?['']:[]);
  if(failed)assert.equal(node.textContent,'Refresh failed');
 }
});
