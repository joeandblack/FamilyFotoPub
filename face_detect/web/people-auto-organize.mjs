import {runPeopleReview,createPeopleActionProgress} from './people-review-progress.mjs';

export function setupAutoOrganize({endpoint,request,after,refresh,scope=()=>null}){
 const run=document.createElement('button');run.type='button';run.textContent='Auto-organize faces';run.id='auto-organize-faces';
 run.title='Match named people and consolidate unnamed groups using all detected faces in To be reviewed';
 const undo=document.createElement('button');undo.type='button';undo.textContent='Undo auto-organize';undo.disabled=true;
 undo.id='undo-auto-organize';run.style.marginInlineStart=undo.style.marginInlineStart='8px';
 const status=document.createElement('span');status.className='muted';status.style.marginInlineStart='16px';
 after.after(run,undo,status);
 const progress=createPeopleActionProgress(status,{inline:true});let busy=false,last=null;
 status.parentElement.style.display='inline-flex';status.parentElement.style.verticalAlign='middle';status.parentElement.style.maxWidth='min(560px,100%)';
 const summary=result=>`${result.matched_faces} faces matched; ${result.groups_before} unnamed groups reduced to ${result.groups_after}.`;
 async function load(){
  try{const data=await request('/auto-organize');last=data.last_run;undo.disabled=busy||!last||last.undone;
   if(!busy)progress.finish(last?(last.undone?'Last Auto-organize run undone.':summary(last.summary)):'');
  }catch{undo.disabled=true;}
 }
 async function execute(action){
  if(busy)return;busy=true;run.disabled=undo.disabled=true;after.disabled=true;
  progress.start(action==='undo'?'Undoing Auto-organize...':'Preparing Auto-organize...');
  try{
   const result=await runPeopleReview(endpoint,{action,...(scope()?{import_id:scope()}:{}),...(action==='undo'?{event_id:last.id}:{})},event=>{
    const labels={waiting:'Waiting for the current operation...',loading:'Loading detected faces...',matching:'Matching named people...',grouping:'Comparing remaining faces...',consolidating:'Consolidating groups...',saving:action==='undo'?'Restoring previous groups...':'Saving groups...'};
    progress.start(labels[event.phase]||'Preparing Auto-organize...');
    if(event.total>0)progress.report({...event,phase:'processing'});
   },{path:'/auto-organize/stream'});
   progress.start('Updating results...');await refresh();
   progress.finish(action==='undo'?`${result.restored} faces restored; ${result.kept_later_changes} later edits kept.`:summary(result));
   const data=await request('/auto-organize');last=data.last_run;
  }catch(error){progress.fail(error);}
  finally{busy=false;run.disabled=false;after.disabled=false;undo.disabled=!last||last.undone;}
 }
 run.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();execute('run');});
 undo.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();execute('undo');});
 load();return {load,isBusy:()=>busy};
}
