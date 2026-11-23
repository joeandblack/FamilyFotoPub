import {size} from './size.mjs';
import {importDuplicates} from './progress.mjs';
import {importStageStatus,statusLabel as label} from './import-status.mjs';
const importName=batch=>batch.source_label||String(batch.source_root||'').replace(/\\/g,'/').replace(/\/$/,'').split('/').pop();
const sortValues={id:b=>b.id,name:importName,photo_files:b=>b.media_totals?.photo?.files||0,photo_bytes:b=>b.media_totals?.photo?.bytes||0,video_files:b=>b.media_totals?.video?.files||0,video_bytes:b=>b.media_totals?.video?.bytes||0,copied:b=>b.counts?.imported||0,dups:b=>importDuplicates(b).count,import_runtime:b=>b.import_status?.active_seconds};
const sorts=new WeakMap();
export function startTime(value){
 if(!value)return '--';
 const date=new Date(value);
 if(!Number.isFinite(date.getTime()))return '--';
 const pad=n=>String(n).padStart(2,'0');
 return `${String(date.getFullYear()).padStart(4,'0')}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
export const overviewColumns=[['ID','id'],['Name','name'],['Start time'],['Import','import_runtime'],['Photo files','photo_files'],['Photo bytes','photo_bytes'],['Video files','video_files'],['Video bytes','video_bytes'],['Copied','copied'],['Dups','dups'],['Unsupported'],['Errors'],['Warnings'],['Video processing'],['Conversion'],['Dup scan'],['Discover people']];
export function sortImports(batches,key='id',direction='desc'){
 const value=sortValues[key]||sortValues.id,multiplier=direction==='asc'?1:-1;
 return [...batches].sort((a,b)=>{
  const av=value(a),bv=value(b);
  if(key==='import_runtime'){
   const missingA=!Number.isFinite(av),missingB=!Number.isFinite(bv);
   if(missingA||missingB)return missingA===missingB?a.id-b.id:missingA?1:-1;
  }
  return multiplier*(key==='name'?av.localeCompare(bv,undefined,{numeric:true,sensitivity:'base'}):av-bv)||a.id-b.id;
 });
}
export function runtime(seconds){
 if(typeof seconds!=='number'||!Number.isFinite(seconds))return '--';
 const minutes=Math.floor(Math.max(0,seconds)/60);return minutes<60?`${minutes}m`:`${Math.floor(minutes/60)}h ${minutes%60}m`;
}
const operation=(status,seconds)=>{const time=runtime(seconds);return time==='--'?status:`${status} ${time}`;};
export function issueCounts(batch,person){
 const stages=Object.values(batch.stages||{});
 const operationErrors=['metadata','conversion','duplicates'].filter(s=>!(batch.stages?.[s]?.error)&&batch[s+'_status']?.error).length;
 return {errors:(batch.counts?.error||0)+stages.reduce((n,c)=>n+(c.error||0),0)+operationErrors+(!batch.counts?.error&&batch.error?1:0)+(person?.state==='blocked'?1:0),
  warnings:stages.reduce((n,c)=>n+(c.warning||0),0)+(person?.errors?.length||0)};
}
export const overviewStage=importStageStatus;
export function overviewRow(batch,pipeline,people){
 const person=people?.[batch.id];
 const peopleState=people===null?'Unavailable':person?label(person.state)||'Not started':'Not started';
 const issues=issueCounts(batch,person);
 const media=batch.media_totals;
 const split=kind=>!media?['--','--']:[Number(media[kind]?.files||0).toLocaleString(),size(media[kind]?.bytes||0)].map(v=>media.unclassified_duplicates?.files?v+' *':v);
 return [String(batch.id),importName(batch),startTime(batch.started_at),
  operation(overviewStage(batch,'import',pipeline),batch.import_status?.active_seconds),
  ...split('photo'),...split('video'),Number(batch.counts?.imported||0).toLocaleString(),
  importDuplicates(batch).count.toLocaleString(),String(batch.counts?.unsupported||0),String(issues.errors),String(issues.warnings),
  ...['metadata','conversion','duplicates'].map(s=>operation(overviewStage(batch,s,pipeline),batch[s+'_status']?.run_seconds??batch[s+'_status']?.active_seconds)+(s==='conversion'&&batch.conversion_pending>0?` · ${Number(batch.conversion_pending).toLocaleString()} pending`:'')),operation(peopleState,person?.run_seconds)];
}
export function renderImportOverview(table,batches,pipeline,people,reviewIssues){
 const doc=table.ownerDocument;
 const node=(tag,text)=>{const el=doc.createElement(tag);if(text!==undefined)el.textContent=text;return el;};
 const head=node('thead'),header=node('tr');
 const sort=sorts.get(table)||{key:'id',direction:'desc'};sorts.set(table,sort);
 for(const [title,key] of overviewColumns){
  const cell=node('th');cell.scope='col';
  if(title==='Errors')cell.title='Current item errors and stopped-operation faults across all stages';
  if(title==='Warnings')cell.title='Warning counts across video processing, conversion, duplicate scanning and People';
  if(title==='Conversion')cell.title='Conversion status, runtime and pending AVI / FLV / MTS; hover for pending format counts';
  if(key){const button=node('button',title);button.type='button';button.className='overview-sort';button.title=key==='import_runtime'?'Sort by import runtime':`Sort by ${title}`;
   cell.ariaSort=sort.key===key?(sort.direction==='asc'?'ascending':'descending'):'none';
   const icon=node('i');icon.dataset.lucide=sort.key===key?(sort.direction==='asc'?'arrow-up':'arrow-down'):'arrow-up-down';button.append(icon);
   button.onclick=()=>{sorts.set(table,{key,direction:sort.key===key&&sort.direction==='asc'?'desc':'asc'});renderImportOverview(table,batches,pipeline,people,reviewIssues);};cell.append(button);
  }else cell.textContent=title;
  header.append(cell);
 }
 head.append(header);const body=node('tbody');
 for(const batch of sortImports(batches,sort.key,sort.direction)){const row=node('tr');
  overviewRow(batch,pipeline,people).forEach((text,index)=>{const cell=node('td',text);if(index===1)cell.title=batch.source_root||text;if(index===2)cell.title='Import start time in your local time zone';if(index===0||(index>=4&&index<=12))cell.className='numeric';if(text==='--')cell.title='Not recorded';if(index>=4&&index<=7)cell.title=batch.media_totals?.unclassified_duplicates?.files?'Known photo/video totals only. Older skipped duplicates have no saved type and are excluded from this split.':'Import source totals, including recorded duplicates and pending files; unrecognized types excluded.';row.append(cell);});body.append(row);
  if(reviewIssues)for(const [index,severity] of [[11,'error'],[12,'warning']]){
   const cell=row.children[index];if(Number(cell.textContent)>0){const button=node('button',cell.textContent);button.className='issue-count';button.title=`Review ${severity}s for import #${batch.id}`;button.onclick=()=>reviewIssues(batch,severity,people?.[batch.id]);cell.replaceChildren(button);}
  }
  const conversion=row.children[overviewColumns.findIndex(([title])=>title==='Conversion')];
  conversion.title='Pending: '+Object.entries(batch.conversion_pending_formats||{AVI:0,FLV:0,MTS:0}).map(([format,count])=>`${format}: ${count}`).join(' · ');
 }
 if(!batches.length){const row=node('tr'),cell=node('td','No imports recorded.');cell.colSpan=overviewColumns.length;row.append(cell);body.append(row);}
 table.replaceChildren(head,body);
 if(batches.some(b=>b.media_totals?.unclassified_duplicates?.files)){const caption=node('caption','* Photo/video totals exclude older skipped duplicates whose types were not recorded.');table.append(caption);}
 globalThis.lucide?.createIcons();
}
