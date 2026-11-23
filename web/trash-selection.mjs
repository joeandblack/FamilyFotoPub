import {setupDownloads} from './downloads.mjs';
import {confirmAction} from './confirm-dialog.mjs';
import {deleteWithProgress} from './deletion-progress.mjs';
import {rangeSelection,setupDragSelection} from './selection.mjs';
import {confirmAlbumTrash} from './album-trash-warning.mjs';

export async function changeSelected(ids,api,action,onChanged,keepGoing=()=>true){
 let deleted=0;
 for(const id of new Set(ids)){
  if(!keepGoing())return {deleted,error:'Selection context changed; remaining items untouched.'};
  try{await api(`/api/media/${id}/trash/${action}`,{method:'POST'});deleted++;onChanged(id);}
  catch(error){return {deleted,error:error.message};}
 }
 return {deleted,error:null};
}
export const deleteSelected=(ids,api,onDeleted)=>changeSelected(ids,api,'delete',onDeleted);

export function setupTrashSelection({api,refresh,albumAction}){
 const grid=document.getElementById('grid'),bar=document.createElement('div');
 bar.id='trashSelection';bar.hidden=true;
 bar.innerHTML='<button data-action="select">Select</button><button data-action="all" hidden>Select page</button><button data-action="clear" hidden>Clear</button><span class="selection-count" role="status" aria-live="polite"></span><button data-action="download" hidden>Download</button><button data-action="delete" hidden>Move to Trash</button><span class="selection-result" role="status" aria-live="polite"></span>';
 for(const [action,label] of Object.entries({album:'Add to album',transfer:'Move to album',remove:'Remove from Album',cover:'Use as cover'})){const b=document.createElement('button');b.dataset.action=action;b.textContent=label;b.className='album-write';b.hidden=true;bar.append(b);b.onclick=async()=>{if(busy)return;busy=true;undoIds=[];drag.cancel();paint();try{await albumAction?.(action,[...selected]);}finally{busy=false;paint();}};}
 const undo=document.createElement('button');undo.dataset.action='undo';undo.className='album-write';undo.textContent='Undo';undo.title='Restore items from the last Move to Trash action in this session';bar.append(undo);
 for(const [level,label] of Object.entries({private:'Make private',shared:'Make shared'})){const b=document.createElement('button');b.dataset.action='visibility-'+level;b.textContent=label;b.title=level==='private'?'Only you and administrators will see the selected items':'Everyone with an account will see the selected items';b.hidden=true;bar.append(b);}
 grid.before(bar);
 const button=action=>bar.querySelector(`[data-action="${action}"]`),result=bar.querySelector('.selection-result');
 bar.append(result);
 const count=bar.querySelector('.selection-count'),summary=document.getElementById('summary');
 if(summary){const line=document.createElement('div');line.className='library-summary-line';summary.before(line);line.append(summary,count);}
 count.hidden=true;
 let selecting=false,busy=false,rows=[],selected=new Set(),view='all',scope=null,epoch=0,anchor=null,undoIds=[];
 const visible=()=>['all','image','video','trash','album'].includes(view);
 const drag=setupDragSelection(grid,{enabled:()=>selecting&&!busy&&visible(),selected:()=>selected,change:value=>{selected=new Set([...value].filter(id=>rows.some(m=>m.id===id)));paint();}});
 function paint(){
  bar.hidden=!visible();bar.dataset.permanent=String(view==='trash');
  undo.disabled=busy||!undoIds.length;
  grid.classList.toggle('selection-mode',selecting&&visible());
  button('select').title='Select items on this page by click, tap or mouse rectangle. Shift adds a range; Ctrl/Cmd-drag toggles.';
  button('select').textContent=selecting?'Done selecting':'Select';button('select').disabled=busy||!rows.length;
  for(const action of ['all','clear','download','delete']){button(action).hidden=!selecting;button(action).disabled=busy||(action==='all'?!rows.length:!selected.size);}
  for(const action of ['album','transfer','remove','cover']){button(action).hidden=!selecting||view==='trash'||(action!=='album'&&view!=='album')||!albumAction;button(action).disabled=busy||(action==='cover'?selected.size!==1:!selected.size);}
  const changeable=rows.filter(m=>selected.has(m.id)&&canChange(m)).length;
  for(const level of ['private','shared']){const b=button('visibility-'+level);b.hidden=!selecting||view==='trash';b.disabled=busy||!changeable;}
  button('delete').textContent=view==='trash'?'Delete permanently':'Move to Trash';
  if(view==='album')button('delete').hidden=true;
  button('delete').title=view==='trash'?'Permanently delete selected items':'Move selected items to Recently Deleted; they can be restored';
  button('download').disabled=busy||!selected.size||rows.some(m=>selected.has(m.id)&&m.deleting_at);
  count.hidden=!selecting||!visible()||!selected.size;
  count.textContent=count.hidden?'':`${selected.size} selected on this page (${rows.length} loaded)`;
  for(const tile of grid.querySelectorAll('[data-id]')){
   const active=selecting&&visible(),checked=selected.has(Number(tile.dataset.id));
   tile.classList.toggle('selectable',active);tile.classList.toggle('selected',active&&checked);
   if(active)tile.setAttribute('aria-pressed',String(checked));else tile.removeAttribute('aria-pressed');
  }
 }
 const canChange=m=>document.body.dataset.role==='admin'||m.owner_name===document.body.dataset.user;
 async function setVisibility(level){
  const ids=rows.filter(m=>selected.has(m.id)&&canChange(m)).map(m=>m.id);
  if(busy||!ids.length)return;
  busy=true;drag.cancel();result.textContent='Saving...';paint();
  try{const outcome=await api('/api/media/visibility',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({media_ids:ids,visibility:level})});result.textContent=`${outcome.updated} made ${level}.`;await refresh();}
  catch(error){result.textContent=error.message;}finally{busy=false;paint();}
 }
 button('visibility-private').onclick=()=>setVisibility('private');
 button('visibility-shared').onclick=()=>setVisibility('shared');
 button('select').onclick=()=>{selecting=!selecting;selected.clear();anchor=null;result.textContent='';paint();};
 button('all').onclick=()=>{selected=new Set(rows.map(m=>m.id));paint();};
 button('clear').onclick=()=>{selected.clear();anchor=null;paint();};
 const download=setupDownloads({api});
 button('download').onclick=()=>download(rows.filter(m=>selected.has(m.id)));
 undo.onclick=async()=>{
  if(busy||!undoIds.length)return;
  busy=true;drag.cancel();result.textContent='Restoring...';paint();
  try{
   const outcome=await changeSelected([...undoIds],api,'restore',id=>{undoIds=undoIds.filter(value=>value!==id);});
   result.textContent=`${outcome.deleted} restored.${outcome.error?' Stopped: '+outcome.error:''}`;
   await refresh();
  }finally{busy=false;paint();}
 };
 button('delete').onclick=async()=>{
  const ids=[...selected],permanent=view==='trash',action=permanent?'delete':'trash',token=epoch;
  if(busy||!ids.length)return;
  if(view==='album')return;
  if(permanent&&!await confirmAction(`Permanently delete ${ids.length} selected item(s)? This cannot be undone. Destination copies will be removed; source files are not affected.`,{title:'Permanently delete photos and videos?',confirmLabel:'Delete permanently',danger:true}))return;
  if(busy||token!==epoch)return;
  busy=true;result.textContent=permanent?'Deleting...':'Checking album membership...';drag.cancel();paint();
  try{
   if(!permanent&&!await confirmAlbumTrash(api,ids)){result.textContent='';return;}
   if(token!==epoch)return;
   undoIds=[];result.textContent=permanent?'Deleting...':'Moving to Trash...';
   const onDeleted=id=>{if(!permanent)undoIds.push(id);if(token===epoch)selected.delete(id);};
   const outcome=permanent?await deleteWithProgress({ids,api,refresh,onDeleted,keepGoing:()=>token===epoch,elements:id=>[...grid.querySelectorAll('[data-id]')].filter(tile=>Number(tile.dataset.id)===id)}):await changeSelected(ids,api,action,onDeleted,()=>token===epoch);
   if(token===epoch)result.textContent=`${outcome.deleted} ${permanent?'permanently deleted':'moved to Recently Deleted'}.${outcome.error?' Stopped: '+outcome.error:''}`;
   if(!permanent)await refresh();
  }catch(error){result.textContent=error.message;}finally{busy=false;paint();}
 };
 grid.addEventListener('keydown',e=>{
  if(!selecting||busy)return;
  if(e.key==='Escape'){e.preventDefault();drag.cancel();selected.clear();selecting=false;paint();}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='a'){e.preventDefault();selected=new Set(rows.map(m=>m.id));paint();}
 });
 return {
  toggle(id,event={}){
   if(!visible()||!selecting)return false;
   if(!busy&&rows.some(m=>m.id===id)){
    if(event.shiftKey)for(const value of rangeSelection(rows.map(m=>m.id),anchor,id))selected.add(value);
    else{selected.has(id)?selected.delete(id):selected.add(id);anchor=id;}
    paint();
   }return true;
  },
  context(next){if(scope!==next){scope=next;rows=[];epoch++;drag.cancel();selecting=false;selected.clear();anchor=null;result.textContent='';paint();}},
  sync(items,nextView){
   if(nextView!==view){epoch++;drag.cancel();selecting=false;selected.clear();anchor=null;result.textContent='';}
   if(rows.map(m=>m.id).join(',')!==items.map(m=>m.id).join(','))drag.cancel();
   rows=items;view=nextView;selected=new Set([...selected].filter(id=>rows.some(m=>m.id===id)));paint();
  }
 };
}
