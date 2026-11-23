import {confirmAction} from './confirm-dialog.mjs';

export function needsPlaybackReview(item){
 const validation=item.video_metadata?.conversion_validation;
 return item.kind==='video'&&!!item.original_relative_path&&['done','warning'].includes(item.conversion_state)&&['review-required','user-reviewed'].includes(validation?.playback)&&!validation.original_deleted_at;
}

export function setupConversionReview({api,openViewer}){
 const node=(tag,text)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;};
 const dialog=node('dialog');dialog.className='people-photo-gallery conversion-review';dialog.setAttribute('aria-label','Conversion playback review');
 const header=node('div');header.className='section-heading';
 const title=node('h3'),close=node('button');close.type='button';close.title='Close';close.setAttribute('aria-label','Close playback review');
 close.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';close.onclick=()=>{if(!busy)dialog.close();};header.append(title,close);
 const message=node('p','Converted videos with retained originals. Full playback verification may have been skipped.'),status=node('p');
 message.className=status.className='muted';status.setAttribute('role','status');
 const controls=node('div');controls.style.cssText='display:flex;align-items:center;gap:8px;flex-wrap:wrap';
 const refresh=node('button','Refresh'),previous=node('button','Previous'),next=node('button','Next'),position=node('span');
 for(const button of [refresh,previous,next])button.type='button';
 controls.append(refresh,previous,position,next);
 const select=node('button','Select'),all=node('button','Select all'),remove=node('button','Delete retained originals'),count=node('span');
 for(const button of [select,all,remove])button.type='button';
 controls.append(select,all,remove,count);
 const grid=node('div');grid.className='grid';dialog.append(header,message,status,controls,grid);document.body.append(dialog);
 const activity=node('div');activity.className='conversion-delete-progress';activity.hidden=true;activity.setAttribute('role','status');
 const activityLabel=node('span'),activityBar=node('progress');activityBar.setAttribute('aria-label','Deleting retained originals');activity.append(activityLabel,activityBar);controls.after(activity);
 let revision=0,items=[],page=1,batch,selecting=false,busy=false,loading=false;
 const selected=new Set();
 dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
 dialog.addEventListener('close',()=>{if(dialog.open)return;revision++;grid.replaceChildren();items=[];});
 dialog.addEventListener('keydown',event=>event.stopPropagation());
 function render(){
  const pages=Math.max(1,Math.ceil(items.length/100));page=Math.min(page,pages);
  previous.disabled=busy||page===1;next.disabled=busy||page===pages;position.textContent=`${page} / ${pages}`;
  refresh.disabled=busy||loading;close.disabled=busy;select.disabled=all.disabled=busy||loading;
  remove.disabled=busy||loading||!selected.size;
  select.textContent=selecting?'Done selecting':'Select';count.textContent=selected.size?`${selected.size} selected`:'';
  grid.replaceChildren(...items.slice((page-1)*100,page*100).map(item=>{
   const card=node('div'),button=node('button');button.type='button';button.className='tile'+(selecting?' selectable':'')+(selected.has(item.id)?' selected':'');button.disabled=busy;button.style.width='100%';button.title=item.name;button.setAttribute('aria-label',`${selecting?'Select':'Play'} ${item.name}`);
   if(selecting)button.setAttribute('aria-pressed',String(selected.has(item.id)));
   const frame=node('span');frame.className='frame';
   if(item.video_metadata?.poster){const image=node('img');image.loading='lazy';image.decoding='async';image.alt='';image.src=`/api/media/${item.id}/thumbnail`;frame.append(image);}
   else {const icon=node('span','\u25b6');icon.className='placeholder';frame.append(icon);}
   button.append(frame);button.onclick=()=>{if(busy)return;if(selecting){selected.has(item.id)?selected.delete(item.id):selected.add(item.id);render();}else openViewer(item.id,{items:[item],page:1,pages:1,filters:{kind:'video'}});};
   const name=node('div',item.name);name.title=item.name;name.style.cssText='overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px';
   card.style.minWidth='0';card.append(button,name);return card;
  }));
 }
 async function load(){
  const ticket=++revision;items=[];page=1;selected.clear();selecting=false;loading=true;render();status.textContent='Loading videos for playback review...';
  try{
   const seen=new Set();
   for(let sourcePage=1;;sourcePage++){
    const result=await api('/api/media?'+new URLSearchParams({kind:'video',import_job:String(batch.id),page:sourcePage}));
    if(ticket!==revision||!dialog.open)return;
    for(const item of result.items)if(needsPlaybackReview(item)&&!seen.has(item.id)){seen.add(item.id);items.push(item);}
    render();
    if(sourcePage>=result.pages)break;
   }
   status.textContent=items.length?`${items.length} videos with retained originals`:'No retained originals available.';
  }catch(error){if(ticket===revision)status.textContent=error.message;}
  finally{if(ticket===revision){loading=false;render();}}
 }
 async function finish(deleteOriginal){
  if(busy||loading||!selected.size)return;
  const ids=[...selected];busy=true;render();
  try{
   if(deleteOriginal&&!await confirmAction(`Permanently delete the retained originals for ${ids.length} selected videos? This cannot be undone. Converted MP4s remain. Your source-folder files are not affected.`,{title:'Delete retained originals?',confirmLabel:'Delete originals',danger:true}))return;
   let completed=0;
   activity.hidden=false;
   for(const id of ids){
    const item=items.find(item=>item.id===id);
    activityLabel.textContent=`Deleting originals: ${completed} / ${ids.length} completed - Verifying and deleting ${item?.name||id}`;
    activityBar.removeAttribute('value');
    await api(`/api/import/${batch.id}/playback-review/${id}/${deleteOriginal?'delete':'keep'}`,{method:'POST'});
    selected.delete(id);items=items.filter(item=>item.id!==id);completed++;render();
   }
   status.textContent=`${completed} videos reviewed. ${deleteOriginal?'Retained originals deleted.':'Originals kept.'}`;
  }catch(error){status.textContent=error.message+' Remaining selections have not been confirmed complete; retry or refresh.';}
  finally{activity.hidden=true;busy=false;render();}
 }
 select.onclick=()=>{selecting=!selecting;if(!selecting)selected.clear();render();};
 all.title='Select all videos in this playback review, across pages';
 all.onclick=()=>{selecting=true;for(const item of items)selected.add(item.id);render();};
 remove.onclick=()=>finish(true);
 previous.onclick=()=>{page--;render();};next.onclick=()=>{page++;render();};refresh.onclick=load;
 return async selected=>{batch=selected;title.textContent=`Import #${batch.id} - Playback review`;if(!dialog.open)dialog.showModal();globalThis.lucide?.createIcons();await load();};
}
