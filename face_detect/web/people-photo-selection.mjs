import {setupDragSelection} from '../../web/selection.mjs';

export function setupPhotoSelection(grid,toolbar,changed){
 let enabled=false,selected=new Set();
 const button=text=>{const b=document.createElement('button');b.type='button';b.textContent=text;toolbar.append(b);return b;};
 const toggle=button('Select'),all=button('Select all'),clear=button('Clear');
 const tiles=()=>[...grid.querySelectorAll('[data-id]')];
 function paint(){
  grid.classList.toggle('selecting-photos',enabled);
  toggle.textContent=enabled?'Done selecting':'Select';toggle.setAttribute('aria-pressed',String(enabled));
  clear.hidden=!enabled;
  if(enabled){grid.setAttribute('role','listbox');grid.setAttribute('aria-multiselectable','true');}
  else{grid.removeAttribute('role');grid.removeAttribute('aria-multiselectable');}
  for(const tile of tiles()){
   tile.classList.toggle('photo-selected',selected.has(Number(tile.dataset.id)));tile.tabIndex=enabled?0:-1;
   if(enabled){tile.setAttribute('role','option');tile.setAttribute('aria-selected',String(selected.has(Number(tile.dataset.id))));}
   else{tile.removeAttribute('role');tile.removeAttribute('aria-selected');}
  }
  changed(selected);
 }
 const drag=setupDragSelection(grid,{enabled:()=>enabled,selected:()=>selected,change:value=>{selected=value;paint();},overlayParent:grid,scrollContainer:()=>grid.closest('dialog')||grid,plain:'add'});
 function choose(event){
  if(!enabled)return;
  event.preventDefault();event.stopImmediatePropagation();
  const tile=event.target.closest('[data-id]');if(!tile)return;
  const id=Number(tile.dataset.id);selected.has(id)?selected.delete(id):selected.add(id);paint();
 }
 grid.addEventListener('click',choose,true);
 grid.addEventListener('keydown',event=>{if([' ','Enter'].includes(event.key))choose(event);});
 toggle.onclick=()=>{drag.cancel();enabled=!enabled;if(!enabled)selected.clear();paint();};
 all.onclick=()=>{drag.cancel();enabled=true;selected=new Set(tiles().map(t=>Number(t.dataset.id)));paint();};
 clear.onclick=()=>{drag.cancel();selected.clear();paint();};
 paint();
 return {clear(){drag.cancel();selected.clear();paint();}};
}
