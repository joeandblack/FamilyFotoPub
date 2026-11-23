let active=false;

// Keep the existing per-file API: only acknowledged deletions count as completed.
export async function deleteWithProgress({ids,api,elements=()=>[],refresh,onDeleted=()=>{},keepGoing=()=>true}){
 if(active)return {deleted:0,error:'Another deletion is in progress.'};
 active=true;
 const unique=[...new Set(ids)],hidden=new Map(),completed=new Set(),previous=document.activeElement;
 const dialog=document.createElement('dialog');dialog.className='ff-dialog confirm-dialog deletion-progress';
 dialog.setAttribute('aria-labelledby','deletion-heading');dialog.setAttribute('aria-describedby','deletion-status');
 const title=document.createElement('h2');title.id='deletion-heading';title.textContent='Deleting in progress';
 const status=document.createElement('p');status.id='deletion-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');
 const current=document.createElement('div');current.className='deletion-current';
 const preview=document.createElement('canvas');preview.width=96;preview.height=72;preview.setAttribute('role','img');preview.setAttribute('aria-label','Item being deleted');
 const placeholder=document.createElement('span');placeholder.className='deletion-preview-placeholder';placeholder.textContent='No preview';
 const frame=document.createElement('div');frame.className='deletion-preview';frame.append(preview,placeholder);
 const label=document.createElement('span');label.className='deletion-current-label';current.append(label,frame);
 const progress=document.createElement('progress');progress.max=unique.length;progress.value=0;progress.setAttribute('aria-label','Permanently deleted items');
 const note=document.createElement('p');note.className='deletion-note';note.textContent='Please keep this page open. Other actions are temporarily unavailable.';
 dialog.append(title,status,current,progress,note);
 const showCurrent=id=>{
  const sources=[...hidden].filter(([,item])=>item.id===id).map(([element])=>element);
  // Duplicate-review cards also contain the kept copy: never use that second image.
  const candidate=sources.flatMap(element=>element.matches('img')?[element]:[...element.querySelectorAll('img')])[0];
  const image=candidate?.complete&&candidate.naturalWidth>0?candidate:null;
  label.textContent=`Deleting item ${completed.size+1} of ${unique.length}`;
  preview.hidden=!image;placeholder.hidden=!!image;
  if(!image)return;
  // Draw the already-decoded image; never fetch or wait for a preview during deletion.
  try{
   const context=preview.getContext('2d'),scale=Math.min(preview.width/image.naturalWidth,preview.height/image.naturalHeight);
   context.clearRect(0,0,preview.width,preview.height);
   const width=image.naturalWidth*scale,height=image.naturalHeight*scale;
   context.drawImage(image,(preview.width-width)/2,(preview.height-height)/2,width,height);
  }catch{preview.hidden=true;placeholder.hidden=false;}
 };
 const update=()=>{status.textContent=`${completed.size} of ${unique.length} permanently deleted`;progress.value=completed.size;};
 const preventUnload=event=>{event.preventDefault();event.returnValue='';};
 const blockKeys=event=>{if(event.key==='Escape'||!dialog.contains(event.target)){event.preventDefault();event.stopImmediatePropagation();}};
 dialog.addEventListener('cancel',event=>event.preventDefault());
 let error=null;
 try{
  for(const id of unique)for(const element of elements(id)){hidden.set(element,{id,wasHidden:element.hidden});element.hidden=true;}
  (document.fullscreenElement||document.body).append(dialog);update();dialog.showModal();dialog.tabIndex=-1;dialog.focus();
  window.addEventListener('beforeunload',preventUnload);
  window.addEventListener('keydown',blockKeys,true);
  if(unique.length)showCurrent(unique[0]);
  // Give the removed tiles and modal a rendered frame before any network work.
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  for(const id of unique){
   if(!keepGoing()){error='The selection context changed. Remaining items were not deleted.';break;}
   showCurrent(id);
   try{await api(`/api/media/${id}/trash/delete`,{method:'POST'});}
   catch(failure){error=failure.message;break;}
   completed.add(id);update();onDeleted(id);
  }
  current.hidden=true;
  for(const [element,item] of hidden)if(!completed.has(item.id))element.hidden=item.wasHidden;
  note.textContent='Updating the photo list...';
  try{await refresh();}catch(failure){error=[error,`Could not refresh the list: ${failure.message}. Refresh the page to verify the latest state.`].filter(Boolean).join(' ');}
  if(error){
   title.textContent='Deletion stopped';note.textContent=error+' Items not confirmed deleted remain available for review.';
   const actions=document.createElement('div');actions.className='confirm-dialog-actions';
   const close=document.createElement('button');close.type='button';close.textContent='Close';actions.append(close);dialog.append(actions);
   await new Promise(resolve=>{close.onclick=resolve;close.focus();});
  }
  return {deleted:completed.size,error};
 }finally{
  for(const [element,item] of hidden)if(!completed.has(item.id))element.hidden=item.wasHidden;
  window.removeEventListener('beforeunload',preventUnload);window.removeEventListener('keydown',blockKeys,true);
  dialog.close();dialog.remove();active=false;
  if(previous?.isConnected&&!previous.disabled)previous.focus({preventScroll:true});
 }
}
