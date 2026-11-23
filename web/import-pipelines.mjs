import {size} from './size.mjs';
import {showNotice} from './confirm-dialog.mjs';
import {importIssues,appendImportIssues} from './issues.mjs';
import {hoursMinutes,createVideoEstimateDisplay,createEstimateDisplay,importDuplicates} from './progress.mjs';
import {stageProgress,importStageStatus,statusLabel} from './import-status.mjs';
export {stageProgress} from './import-status.mjs';
const estimates=new Map();
const activeImports=new WeakMap();
const summaryContexts=new WeakMap();
export function peoplePercent(people){
  const total=Number(people?.total)||0;if(total<=0)return '';
  return `${Math.min(100,Math.max(0,((Number(people.done)||0)+(Number(people.excluded)||0))/total*100)).toFixed(1)}%`;
}
export function updateImportPeopleStatus(container,people){
  const context=summaryContexts.get(container);if(!context)return;
  for(const group of container.querySelectorAll('.import-group')){
    const batch=context.batches.find(b=>String(b.id)===group.dataset.job);
    const badge=group.querySelector('.import-summary-status');if(!batch||!badge)continue;
    const activity=importSummary(batch,context.pipeline,people?.[batch.id]);
    badge.textContent=activity.text;
    badge.className=`import-summary-status${activity.running?' is-running':''}`;
  }
}
export function importSummary(batch,pipeline,people){
  const names={import:'Importing media',metadata:'Processing videos',conversion:'Converting videos',duplicates:'Scanning photo duplicates'};
  const labels=[];let running=false;
  for(const stage of ['import','metadata','conversion','duplicates']){
    const state=importStageStatus(batch,stage,pipeline);
    if(['Running','Scanning','Pausing...'].includes(state)){labels.push(`${state==='Pausing...'?'Pausing: ':''}${names[stage]}`);running=true;}
    else if(state==='Waiting to start')labels.push(`Waiting: ${names[stage]}`);
  }
  if(people){
    const states={running:'Running Discover people',preparing:'Preparing Discover people',queued:'Discover people queued','waiting-for-foreground':'Discover people waiting','awaiting-receipt':'Discover people saving results','waiting-service':'Discover people waiting for service',paused:'Discover people paused',blocked:'Discover people stopped with error','paused-error':'Discover people stopped with error'};
    if(states[people.state])labels.push(states[people.state]+(peoplePercent(people)?` · ${peoplePercent(people)}`:''));
    if(['running','preparing','queued','waiting-for-foreground','awaiting-receipt'].includes(people.state))running=true;
  }
  if(!labels.length){
    for(const stage of ['conversion','metadata','duplicates']){
      const state=importStageStatus(batch,stage,pipeline);
      if(['Paused','Stopped with error','Blocked','Waiting for face detection service','Interrupted'].includes(state))labels.push(`${names[stage]}: ${state.toLowerCase()}`);
    }
  }
  return {text:labels.join(' · ')||(batch.state==='completed-review-required'?'Imported':importStageStatus(batch,'import',pipeline)),running};
}

export function organizeImportSections(container,batches){
  const doc=container.ownerDocument,manual=doc.getElementById('manualImport');
  const focused=doc.activeElement;
  const currentIds=new Set(batches.filter(b=>!['completed-review-required','cancelled'].includes(b.state)).map(b=>String(b.id)));
  const groups=[...container.querySelectorAll('.import-group')];
  const make=(title,id)=>{const section=doc.createElement('section');section.className='import-list-section';const heading=doc.createElement('h2');heading.id=id;heading.textContent=title;section.setAttribute('aria-labelledby',id);section.append(heading);return section;};
  const current=make(currentIds.size?'Current Import':'New Import','currentImportHeading');
  const previous=make('Previous Imports','previousImportsHeading');
  // Retain the actual source picker, including entered paths and disclosure state.
  if(manual){manual.hidden=currentIds.size>0&&!batches.some(b=>currentIds.has(String(b.id))&&b.source_kind==='upload');current.append(manual);}
  for(const group of groups)(currentIds.has(group.dataset.job)?current:previous).append(group);
  if(!previous.querySelector('.import-group')){const empty=doc.createElement('p');empty.className='muted';empty.textContent='No previous imports.';previous.append(empty);}
  container.replaceChildren(current,previous);
  if(focused&&manual?.contains(focused)&&!manual.hidden)focused.focus({preventScroll:true});
}
export function renderPipelines(container,batches,pipeline,callbacks){
  const {command,options,issues={imports:[]},viewLibrary,createAlbum,reviewDuplicates,trashOptions,peopleReview,reviewIssues,reviewPlayback,loadDetails,organizeSections=true,emptyText='No imports recorded.'}=callbacks;
  summaryContexts.set(container,{batches,pipeline});
  container.dispatchEvent?.(new CustomEvent('pipeline-status',{detail:pipeline}));
  const doc=container.ownerDocument;
  const previousGroups=new Map([...container.querySelectorAll('.import-group')].map(g=>[g.dataset.job,g]));
  const manual=organizeSections?doc.getElementById?.('manualImport'):null;
  const sourceFocus=manual?.contains(doc.activeElement)?doc.activeElement:null;
  if(manual&&container.contains(manual))container.before(manual);
  const opened=new Set([...container.querySelectorAll('details[open]')].map(e=>e.dataset.job));
  const openIssues=new Set([...container.querySelectorAll('details[open]')].map(e=>e.dataset.issueKey).filter(Boolean));
  const focused=doc.activeElement&&container.contains(doc.activeElement)?{job:doc.activeElement.closest('[data-job]')?.dataset.job,option:doc.activeElement.dataset.option}:null;
  const activeJob=pipeline.busy&&!pipeline.face_busy?String(pipeline.job_id):null;
  const activeChanged=!activeImports.has(container)||activeImports.get(container)!==activeJob;
  activeImports.set(container,activeJob);
  container.replaceChildren();
  const element=(tag,text,className)=>{const e=doc.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;};
  function linkCounts(row,batch,stage){
    if(!reviewIssues)return;
    for(const counts of row.children){
      if(counts.className!=='pipeline-counts')continue;
      const text=counts.textContent;counts.textContent='';
      for(const part of text.split(/((?:Warnings|Errors) \d+)/)){
        const match=/^(Warnings|Errors) ([1-9]\d*)$/.exec(part);
        if(!match){counts.append(doc.createTextNode(part));continue;}
        const link=element('button',part,'issue-count');link.type='button';
        link.title=`Review ${stage} ${match[1].toLowerCase()} for import #${batch.id}`;
        link.onclick=()=>reviewIssues(batch,match[1]==='Warnings'?'warning':'error',peopleReview?.status?.[batch.id],stage);counts.append(link);
      }
    }
  }
  for(const batch of batches){
    const group=element('details',undefined,'import-group');group.dataset.job=String(batch.id);
    const files=importIssues(issues,batch.id);
    group.open=activeChanged?String(batch.id)===activeJob:opened.has(String(batch.id));
    const name=batch.source_label||batch.source_root.replace(/\\/g,'/').replace(/\/$/,'').split('/').pop();
    const activity=importSummary(batch,pipeline,peopleReview?.status?.[batch.id]);
    const summary=element('summary',`${batch.source_kind==='upload'?'Upload':'Server'} · ${name}${batch.source_kind==='upload'&&batch.uploader?' · by '+batch.uploader:''} #${batch.id}`);
    const badge=element('span',activity.text,`import-summary-status${activity.running?' is-running':''}`);
    summary.append(badge);group.append(summary);
    if(batch.summary_only&&loadDetails){
      const message=element('p','Loading import details...','muted');message.setAttribute('role','status');group.append(message);
      const previous=previousGroups.get(String(batch.id));
      if(group.open&&previous?.dataset.detailsLoaded){message.remove();group.append(...[...previous.childNodes].slice(1));group.dataset.detailsLoaded='true';}
      let loading=false,loaded=false;
      const expand=async()=>{
        if(!group.open||loading||loaded)return;loading=true;
        try{
          const result=await loadDetails(batch.id);if(!group.isConnected)return;
          const detail=result.imports.find(b=>b.id===batch.id);if(!detail)throw Error('Import no longer available.');
          const temporary=element('div');
          renderPipelines(temporary,[detail],result.pipeline||pipeline,{...callbacks,issues:result.issues,loadDetails:null,organizeSections:false});
          const rendered=temporary.querySelector('.import-group');group.replaceChildren(...rendered.childNodes);group.dataset.detailsLoaded='true';loaded=true;
          globalThis.lucide?.createIcons();
        }catch(error){if(group.isConnected){message.textContent=`${error.message} Close and reopen to retry.`;if(!message.isConnected)group.append(message);}}
        finally{loading=false;}
      };
      group.addEventListener('toggle',expand);container.append(group);
      if(group.open)queueMicrotask(expand);
      continue;
    }
    group.append(element('div',`${batch.source_kind==='upload'?'Selected folder: '+name:batch.source_root} · ${new Date(batch.started_at).toLocaleString()}`,'muted'));
    if(viewLibrary){const link=element('button','View in Library');link.type='button';link.onclick=()=>viewLibrary(batch.id);group.append(link);}
    if(createAlbum){const button=element('button','Create album');button.type='button';button.className='album-write';button.onclick=()=>createAlbum(batch.id);group.append(button);}
    if(batch.state==='cancelled'){group.append(element('p','Cancelled import'));appendImportIssues(group,files,batch.id,openIssues);container.append(group);continue;}
    const optionValues={auto_metadata:!!batch.auto_metadata,auto_conversion:!!batch.auto_conversion,auto_duplicates:!!batch.auto_duplicates};
    const optionInputs=[];
    const optionStages=[['auto_metadata','Process videos after import'],['auto_conversion','Convert AVI / FLV / MTS after video processing'],['auto_duplicates','Photo duplicate scan']];
    for(const [stage,title] of [['import','Photo / video import'],['metadata','Video processing'],['conversion','Video conversion'],['duplicates','Photo duplicate scan']]){
      const p=stageProgress(batch,stage),row=element('section',undefined,'pipeline-stage');
      const active=pipeline.busy&&!pipeline.face_busy&&pipeline.job_id===batch.id&&pipeline.stage===stage;
      const handoff=pipeline.handoff;
      const waiting=handoff?.state==='pending'&&handoff.job_id===batch.id&&handoff.stage===stage;
      const status=batch[stage+'_status'];
      const complete=stage==='import'?batch.state==='completed-review-required':p.total>0&&!p.pending;
      const head=element('div',undefined,'pipeline-heading');
      const state=importStageStatus(batch,stage,pipeline);
      head.append(element('strong',title),element('span',`${state} · ${p.percent.toFixed(1)}%`));
      const button=element('button');button.type='button';
      button.innerHTML=waiting?'Cancel pending start':active?'<i data-lucide="pause"></i> Pause':'<i data-lucide="play"></i> Start / Resume';
      button.title=`${active?'Pause':'Start or resume'} ${title} for ${name}`;
      const prerequisites=(stage==='import'&&batch.source_kind!=='upload')||(batch.state==='completed-review-required'&&(stage!=='conversion'||!stageProgress(batch,'metadata').pending));
      button.disabled=waiting?false:active?pipeline.pause_requested:(pipeline.busy&&!pipeline.face_busy&&!(pipeline.duplicate_busy&&stage==='import'))||handoff?.state==='pending'||complete||!prerequisites||(stage!=='import'&&!p.pending);
      if(active&&pipeline.pause_requested)button.textContent='Pausing...';
      button.onclick=()=>command(batch.id,(active||waiting)?'pause':stage+'/resume',button);
      head.append(button);row.append(head);
      if(stage==='duplicates'&&reviewDuplicates){const review=element('button','Review matches');review.type='button';review.onclick=()=>reviewDuplicates(batch.id).catch(e=>showNotice(e.message));head.append(review);}
      if(stage==='duplicates'&&reviewDuplicates){const review=element('button','Review trashed duplicates');review.type='button';review.onclick=()=>reviewDuplicates(batch.id,'trashed').catch(e=>showNotice(e.message));row.append(review);}
      if(handoff?.job_id===batch.id&&handoff.stage===stage&&handoff.state==='failed')row.append(element('p',handoff.error||'Pending start failed; request it again.','pipeline-error'));
      if(p.percent>0&&p.percent<100){const progress=element('progress');progress.max=100;progress.value=p.percent;progress.setAttribute('aria-label',title+' completion');row.append(progress);}
      const c=p.counts;
      if(c.skipped)row.append(element('div',`Reviewed - skipped: ${c.skipped}`,'muted'));
      row.append(element('div',stage==='import'?`${batch.total.toLocaleString()} files / ${size(batch.total_bytes)} · Copied ${c.imported||0} · Duplicates ${importDuplicates(batch).count}${c.unsupported?` · Unsupported ${c.unsupported}`:''} · Pending ${c.pending||0} · Errors ${c.error||0}`:`Done ${c.done||0} · Warnings ${c.warning||0}${stage==='conversion'?` - Output duplicates ${c.duplicate||0}`:''}${stage==='metadata'?` · Ancillary ${c.ancillary||0}`:''} · Pending ${p.pending}`,'pipeline-counts'));
      if(status){
        if(stage==='conversion'&&status.execution_policy?.retained_original_validation==='quick'){
          row.append(element('div','MTS / FLV: fast verification · Originals retained','muted'));
          if(reviewPlayback){const review=element('button','Review playback','issue-count');review.type='button';review.onclick=()=>reviewPlayback(batch);row.append(review);}
        }
        if(stage==='duplicates'&&status.trashed_duplicates)row.append(element('div',`Moved to Trash: ${status.trashed_duplicates}`,'muted'));
        const key=batch.id+stage;if(!estimates.has(key))estimates.set(key,stage==='import'?createEstimateDisplay():createVideoEstimateDisplay());
        const estimate=['Running','Scanning'].includes(state)?estimates.get(key)(status,active):state==='Not applicable'?'':state;
        row.append(element('div',`Run ${hoursMinutes(stage==='import'?status.active_seconds:status.run_seconds)}${estimate?' · '+estimate:''}`,'muted'));
        if(stage==='import'){
          const bytesTotal=batch.total_bytes,bytesDone=status.processed_bytes;
          row.append(element('div',`Bytes ${size(bytesDone)} / ${size(bytesTotal)} · ${bytesTotal?(bytesDone/bytesTotal*100).toFixed(1):'0.0'}%`,'muted'));
          if(active&&status.current)row.append(element('div',`${status.current} · ${size(status.current_size||0)}`,'pipeline-file'));
        }
        if(active&&status.current_file)row.append(element('div',`${status.current_file.name} · ${size(status.current_file.size)}`,'pipeline-file'));
        if(active&&stage==='conversion'&&status.current_step)row.append(element('div',status.current_step,'muted'));
        if(status.error)row.append(element('div',status.error,'pipeline-error'));
      }
      if(stage==='duplicates'&&trashOptions){
        const wrap=element('label',undefined,'pipeline-options write-action'),input=element('input');input.type='checkbox';input.checked=batch.auto_trash_duplicates!==false;
        input.onchange=()=>trashOptions(batch.id,input.checked,input);
        wrap.append(input,doc.createTextNode('Move clear duplicates to Trash'));row.append(wrap);
      }
      if(stage!=='duplicates'&&!complete&&!(stage!=='import'&&batch.state==='completed-review-required'&&!p.total)){
        const switches=element('div',undefined,'pipeline-options');
        const start={import:0,metadata:1,conversion:2}[stage];
        for(const [key,label] of optionStages.slice(start)){
          const target=stageProgress(batch,key==='auto_metadata'?'metadata':key==='auto_conversion'?'conversion':'duplicates');
          if((target.total>0&&!target.pending)||(batch.state==='completed-review-required'&&!target.total))continue;
          const wrap=element('label'),input=element('input');input.type='checkbox';input.dataset.option=key;input.checked=optionValues[key];
          input.disabled=batch.state==='completed-review-required'&&!stageProgress(batch,key==='auto_metadata'?'metadata':key==='auto_conversion'?'conversion':'duplicates').total;
          optionInputs.push(input);wrap.append(input,doc.createTextNode(label));switches.append(wrap);
          input.onchange=async()=>{
            const previous={...optionValues};optionValues[key]=input.checked;
            if(key==='auto_conversion'&&input.checked)optionValues.auto_metadata=true;
            if(key==='auto_metadata'&&!input.checked)optionValues.auto_conversion=false;
            for(const other of optionInputs)other.checked=optionValues[other.dataset.option];
            try{await options(batch.id,{...optionValues},input);}
            catch(error){Object.assign(optionValues,previous);for(const other of optionInputs)other.checked=optionValues[other.dataset.option];throw error;}
          };
        }
        if(switches.children.length)row.append(switches);
      }
      if(stage!=='import'&&(c.error||status?.error))row.append(element('div',`Errors ${c.error||1}`,'pipeline-counts'));
      linkCounts(row,batch,{import:'Import',metadata:'Video processing',conversion:'Video conversion',duplicates:'Photo duplicate scan'}[stage]);group.append(row);
    }
    if(peopleReview){
      const row=element('section',undefined,'pipeline-stage');
      const s=peopleReview.status?.[batch.id]||{state:'not-started',done:0,total:0};
      const active=['running','queued','waiting-for-foreground','preparing'].includes(s.state);
      const labels={'preparing':'Preparing scan','start-error':'Not started','waiting-for-foreground':'Waiting for app activity','waiting-service':'Waiting for face detection service','awaiting-receipt':'Paused before saving results'};
      const head=element('div',undefined,'pipeline-heading');head.append(element('strong','Discover people'),element('span',(labels[s.state]||statusLabel(s.state))+(peoplePercent(s)?` · ${peoplePercent(s)}`:'')));
      const button=element('button','Start / Resume');button.type='button';
      button.disabled=active||s.state==='completed'||batch.state!=='completed-review-required'||pipeline.busy;
      button.onclick=()=>peopleReview.command(batch.id,'resume',button);head.append(button);
      const pause=element('button','Pause');pause.type='button';pause.disabled=!active||s.state==='preparing';
      pause.onclick=()=>peopleReview.command(batch.id,'pause',pause);head.append(pause);
      const review=element('button','Review people');review.type='button';review.onclick=()=>peopleReview.review(batch.id);head.append(review);row.append(head);
      const completed=(s.done||0)+(s.excluded||0);
      if(completed>0&&completed<s.total){const progress=element('progress');progress.max=s.total;progress.value=completed;progress.setAttribute('aria-label','Discover people completion');row.append(progress);}
      row.append(element('div',`${s.done||0} / ${s.total||0} photos · Excluded ${s.excluded||0} · Warnings ${s.errors?.length||0}`,'pipeline-counts'));
      if(s.message){const message=element('p',s.message,'people-startup-message muted');message.setAttribute('role','status');row.append(message);}
      if(s.state==='waiting-service')row.append(element('p','Face detection is unavailable. Open Settings, start the face detection service, then resume this scan when it is ready.','people-startup-message muted'));
      if(s.current_media_id)row.append(element('div',`Current photo #${s.current_media_id}`,'muted'));
      if(s.state==='blocked')row.append(element('div','Errors 1','pipeline-counts'));
      linkCounts(row,batch,'Discover people');group.append(row);
    }
    container.append(group);
    if(pipeline.job_id===batch.id&&pipeline.error)group.append(element('div',pipeline.error,'pipeline-error'));
    appendImportIssues(group,files,batch.id,openIssues);
  }
  if(!batches.length)container.textContent=emptyText;
  if(organizeSections&&doc.getElementById)organizeImportSections(container,batches);
  if(sourceFocus)sourceFocus.focus({preventScroll:true});
  if(focused?.option)container.querySelector(`[data-job="${focused.job}"] [data-option="${focused.option}"]`)?.focus({preventScroll:true});
}
