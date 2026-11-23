import {confirmAction} from './confirm-dialog.mjs';
import {peopleWarningLabel,peopleWarningDetail} from '../face_detect/web/people-scan-warnings.mjs';
export function setupImportDiagnostics(api){
 const dialog=document.createElement('dialog');dialog.className='import-diagnostics';dialog.setAttribute('aria-label','Import warnings and errors');document.body.append(dialog);
 const node=(tag,text)=>{const el=document.createElement(tag);el.textContent=text;return el;};
 let revision=0;
 dialog.addEventListener('close',()=>revision++);
 return async(batch,severity,person,stageFilter=null)=>{
  const ticket=++revision,heading=node('h3',`Import #${batch.id}${stageFilter?' - '+stageFilter:''} - ${severity==='warning'?'Warnings':'Errors'}`),close=node('button','Close'),body=node('div','Loading...');
  close.onclick=()=>dialog.close();dialog.replaceChildren(heading,close,body);if(!dialog.open)dialog.showModal();
  try{
   const rows=await api(`/api/import/${batch.id}/diagnostics`);
   if(ticket!==revision)return;
   if(person?.state==='blocked')rows.push({stage:'Discover people',severity:'error',error:person.error||person.message||'Discover people is blocked',name:''});
   const savedPeopleIds=new Set(rows.filter(r=>r.stage==='Discover people'&&r.severity==='warning').map(r=>r.media_id));
   for(const e of person?.errors||[])if(!savedPeopleIds.has(e.media_id))rows.push({stage:'Discover people',severity:'warning',...e,name:e.name||`Media #${e.media_id}`});
   const groups=new Map();
   const visible=rows.filter(r=>r.severity===severity&&(!stageFilter||r.stage===stageFilter));
   for(const r of visible){
    let reason=r.error||'No details recorded';
    if(r.stage==='Discover people')reason=peopleWarningLabel(r.error);
    if(r.stage==='Photo duplicate scan'&&r.severity==='warning'&&/^image file is truncated(?: \(\d+ bytes not processed\))?$/i.test(reason))reason='Image file is truncated';
    if(r.stage==='Photo duplicate scan'&&r.severity==='warning'&&reason.startsWith('Large image scan failed: '))reason='Large image scan failed';
    const key=JSON.stringify([r.stage,reason]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(r);
   }
   body.replaceChildren();
   if(severity==='warning'&&visible.some(r=>r.stage==='Photo duplicate scan')){
    const scan=node('button','Scan unexamined photos'),status=node('p','');scan.type='button';
    scan.onclick=async()=>{
     scan.disabled=true;
     try{const result=await api(`/api/import/${batch.id}/photo-duplicates/unexamined`,{method:'POST'});status.textContent=result.message+' Use Photo duplicate scan Pause/Resume and Review matches in this import.';}
     catch(error){status.textContent=error.message;scan.disabled=false;}
    };body.append(scan,status);
   }
   if(!groups.size)body.textContent='No current issues recorded.';
   for(const [key,rows] of groups){const [stage,reason]=JSON.parse(key),details=document.createElement('details');details.append(node('summary',`${stage}: ${reason} (${rows.length})`));
    if(stage==='Discover people'&&rows.some(row=>row.media_id)){
     details.append(node('p','Skipped for face detection; original files are retained.'));
     const legacy=rows.find(row=>row.media_id&&!row.detail);
     if(legacy)details.append(node('p',peopleWarningDetail(legacy)));
    }
    if(stage==='Photo duplicate scan'&&severity==='warning'&&['Image exceeds manual review limit of 60 megapixels','Large image scan failed'].includes(reason)){
     const actions=document.createElement('div'),status=node('p',''),scan=node('button','Scan large image'),skip=node('button','Reviewed - skip');
     for(const button of [scan,skip])button.type='button';
     const act=async(skipping)=>{
      if(skipping&&!await confirmAction(`Skip duplicate comparison for these ${rows.length} photos? Files will be retained, but not checked for duplicates.`,{title:'Skip duplicate comparison?',confirmLabel:'Reviewed - skip'}))return;
      scan.disabled=skip.disabled=true;
      try{const result=await api(`/api/import/${batch.id}/photo-duplicates/large-images${skipping?'/skip':''}`,{method:'POST'});status.textContent=result.message;if(skipping)details.remove();}
      catch(error){status.textContent=error.message;scan.disabled=skip.disabled=false;}
     };
     scan.onclick=()=>act(false);skip.onclick=()=>act(true);actions.append(scan);if(reason==='Large image scan failed')actions.append(skip);actions.append(status);body.append(actions);
    }
    const list=document.createElement('ul');
    for(const row of rows){
     const isPeople=stage==='Discover people'&&row.media_id;
     const name=isPeople?(row.name&&row.name!==`Media #${row.media_id}`?`${row.name} (Media #${row.media_id})`:`Media #${row.media_id}`):(row.name||'Operation-level issue');
     const item=node('li',name+(reason==='Large image scan failed'?`: ${row.error.slice('Large image scan failed: '.length)}`:''));
     if(isPeople&&row.detail){
      item.append(node('div',peopleWarningDetail(row)));
     }
     list.append(item);
    }
    details.append(list);body.append(details);}
  }catch(error){if(ticket===revision)body.textContent=error.message;}
 };
}
