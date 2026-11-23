import {confirmAction} from './confirm-dialog.mjs';

const RETAINED_FORMATS=['.avi','.flv','.mts'];
const DELETABLE_PLAYBACK=['review-required','user-reviewed','decoded',undefined,null];

// The server's retained-originals list is authoritative (it checks the disk); this only narrows it.
export function needsPlaybackReview(item){
 const validation=item.video_metadata?.conversion_validation||{};
 const original=String(item.original_relative_path||'').toLowerCase();
 return item.kind==='video'&&RETAINED_FORMATS.some(ext=>original.endsWith(ext))&&['done','warning'].includes(item.conversion_state)&&DELETABLE_PLAYBACK.includes(validation.playback)&&!validation.original_deleted_at;
}

export function setupConversionReview({api,openViewer}){
 const node=(tag,text)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;};
 const dialog=node('dialog');dialog.className='people-photo-gallery conversion-review';dialog.setAttribute('aria-label','Conversion playback review');
 const header=node('div');header.className='section-heading';
 const title=node('h3'),close=node('button');close.type='button';close.title='Close';close.setAttribute('aria-label','Close playback review');
 close.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';close.onclick=()=>{if(!busy)dialog.close();};header.append(title,close);
 const message=node('p','Converted videos whose AVI, FLV or MTS original is still kept. Play one to check the MP4 before deleting its original. Full playback verification may have been skipped for MTS and FLV.'),status=node('p');
 message.className=status.className='muted';status.setAttribute('role','status');
 const controls=node('div');controls.style.cssText='display:flex;align-items:center;gap:8px;flex-wrap:wrap';
 const refresh=node('button','Refresh'),previous=node('button','Previous'),next=node('button','Next'),position=node('span');
 for(const button of [refresh,previous,next])button.type='button';
 controls.append(refresh,previous,position,next);
 const select=node('button','Select'),all=node('button','Select all'),remove=node('button','Delete retained originals'),stop=node('button','Stop'),count=node('span');
 for(const button of [select,all,remove,stop])button.type='button';
 stop.title='Stop after the video being deleted now finishes';stop.hidden=true;
 controls.append(select,all,remove,stop,count);
 const grid=node('div');grid.className='grid';dialog.append(header,message,status,controls,grid);document.body.append(dialog);
 const activity=node('div');activity.className='conversion-delete-progress';activity.hidden=true;activity.setAttribute('role','status');
 const activityLabel=node('span'),activityBar=node('progress');activityBar.setAttribute('aria-label','Deleting retained originals');activity.append(activityLabel,activityBar);controls.after(activity);
 let revision=0,items=[],page=1,batch,selecting=false,busy=false,loading=false,stopping=false;
 const selected=new Set(),cards=new Map();
 dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
 dialog.addEventListener('close',()=>{if(dialog.open)return;revision++;grid.replaceChildren();items=[];cards.clear();});
 dialog.addEventListener('keydown',event=>event.stopPropagation());
 const pageCount=()=>Math.max(1,Math.ceil(items.length/100));
 const pageItems=()=>items.slice((page-1)*100,page*100);
 // Each tile is built once and kept, so its thumbnail is not fetched again while other tiles change.
 function card(item){
  if(cards.has(item.id))return cards.get(item.id);
  const card=node('div'),button=node('button');button.type='button';button.style.width='100%';button.title=item.name;
  const frame=node('span');frame.className='frame';
  if(item.video_metadata?.poster){const image=node('img');image.loading='lazy';image.decoding='async';image.alt='';image.src=`/api/media/${item.id}/thumbnail`;frame.append(image);}
  else {const icon=node('span','▶');icon.className='placeholder';frame.append(icon);}
  button.append(frame);button.onclick=()=>{if(busy)return;if(selecting){selected.has(item.id)?selected.delete(item.id):selected.add(item.id);paint(item);renderControls();}else openViewer(item.id,{items:[item],page:1,pages:1,filters:{kind:'video'}});};
  const name=node('div',item.name);name.title=item.name;name.style.cssText='overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px';
  card.style.minWidth='0';card.append(button,name);card.button=button;cards.set(item.id,card);return card;
 }
 function paint(item){
  const button=card(item).button,chosen=selected.has(item.id);
  button.className='tile'+(selecting?' selectable':'')+(chosen?' selected':'');button.disabled=busy;
  button.setAttribute('aria-label',`${selecting?'Select':'Play'} ${item.name}`);
  if(selecting)button.setAttribute('aria-pressed',String(chosen));else button.removeAttribute('aria-pressed');
 }
 function renderControls(){
  const pages=pageCount();page=Math.min(page,pages);
  previous.disabled=busy||page===1;next.disabled=busy||page===pages;position.textContent=`${page} / ${pages}`;
  refresh.disabled=busy||loading;close.disabled=busy;select.disabled=all.disabled=busy||loading;
  const deleting=busy&&!activity.hidden;
  remove.disabled=busy||loading||!selected.size;remove.hidden=deleting;
  stop.hidden=!deleting;stop.disabled=stopping;stop.textContent=stopping?'Stopping...':'Stop';
  select.textContent=selecting?'Done selecting':'Select';count.textContent=selected.size?`${selected.size} selected`:'';
 }
 function paintAll(){for(const item of pageItems())paint(item);renderControls();}
 function render(){
  renderControls();
  const shown=pageItems();for(const item of shown)paint(item);
  grid.replaceChildren(...shown.map(card));
 }
 // Drops one finished video: only its tile leaves, and the first tile of the next page moves up into the gap.
 function drop(id){
  const index=items.findIndex(item=>item.id===id);if(index<0)return;
  items.splice(index,1);cards.get(id)?.remove();cards.delete(id);
  if(index<(page-1)*100||page>pageCount()){render();return;}  // An earlier page shrank, or this page emptied.
  const last=items[page*100-1];
  if(last&&!grid.contains(cards.get(last.id))){paint(last);grid.append(card(last));}
  renderControls();
 }
 async function load(){
  const ticket=++revision;items=[];page=1;selected.clear();cards.clear();selecting=false;loading=true;render();status.textContent='Loading videos for playback review...';
  try{
   const seen=new Set(),retained=new Set((await api(`/api/import/${batch.id}/retained-originals`)).ids);
   if(ticket!==revision||!dialog.open)return;
   for(let sourcePage=1;retained.size;sourcePage++){
    const result=await api('/api/media?'+new URLSearchParams({kind:'video',import_job:String(batch.id),page:sourcePage}));
    if(ticket!==revision||!dialog.open)return;
    for(const item of result.items)if(retained.has(item.id)&&needsPlaybackReview(item)&&!seen.has(item.id)){seen.add(item.id);items.push(item);}
    render();
    if(sourcePage>=result.pages)break;
   }
   status.textContent=items.length?`${items.length} videos with retained originals`:'No retained originals available.';
  }catch(error){if(ticket===revision)status.textContent=error.message;}
  finally{if(ticket===revision){loading=false;renderControls();}}
 }
 async function finish(deleteOriginal){
  if(busy||loading||!selected.size)return;
  const ids=[...selected];busy=true;stopping=false;paintAll();
  let completed=0;
  try{
   if(deleteOriginal&&!await confirmAction(`Permanently delete the retained originals for ${ids.length} selected videos? This cannot be undone. Converted MP4s remain. Your source-folder files are not affected.`,{title:'Delete retained originals?',confirmLabel:'Delete originals',danger:true}))return;
   activity.hidden=false;activityBar.max=ids.length;activityBar.value=0;renderControls();
   for(const id of ids){
    if(stopping)break;
    const item=items.find(item=>item.id===id);
    activityLabel.textContent=`Deleting originals: ${completed} / ${ids.length} completed - Verifying and deleting ${item?.name||id}`;
    await api(`/api/import/${batch.id}/playback-review/${id}/${deleteOriginal?'delete':'keep'}`,{method:'POST'});
    selected.delete(id);completed++;activityBar.value=completed;drop(id);
   }
   const left=ids.length-completed;
   status.textContent=left
    ?`Stopped. ${completed} of ${ids.length} retained originals deleted; the other ${left} are still selected.`
    :`${completed} videos reviewed. ${deleteOriginal?'Retained originals deleted.':'Originals kept.'} ${items.length} videos with retained originals left.`;
  }catch(error){status.textContent=`${error.message} ${completed} of ${ids.length} completed. Remaining selections have not been confirmed complete; retry or refresh.`;}
  finally{activity.hidden=true;busy=false;stopping=false;paintAll();}
 }
 stop.onclick=()=>{if(!busy||stopping)return;stopping=true;renderControls();activityLabel.textContent+=' (stopping after this one)';};
 select.onclick=()=>{selecting=!selecting;if(!selecting)selected.clear();paintAll();};
 all.title='Select all videos in this playback review, across pages';
 all.onclick=()=>{selecting=true;for(const item of items)selected.add(item.id);paintAll();};
 remove.onclick=()=>finish(true);
 previous.onclick=()=>{page--;render();grid.scrollTop=0;};next.onclick=()=>{page++;render();grid.scrollTop=0;};refresh.onclick=load;
 return async selected=>{batch=selected;title.textContent=`Import #${batch.id} - Playback review`;if(!dialog.open)dialog.showModal();globalThis.lucide?.createIcons();await load();};
}
