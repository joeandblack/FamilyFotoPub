// One compact destination picker for selected faces and selected groups.
export function bindPersonMenu(button,choices,choose){
 let menu=null;
 const close=()=>{if(menu){menu.hidePopover();menu.remove();menu=null;}button.setAttribute('aria-expanded','false');};
 button.setAttribute('aria-haspopup','menu');button.setAttribute('aria-expanded','false');
 button.onclick=()=>{
  if(button.disabled)return;
  if(menu){close();return;}
  const targets=choices();if(!targets.length)return;
  const doc=button.ownerDocument;
  menu=doc.createElement('div');menu.className='person-destination-menu';menu.setAttribute('popover','auto');menu.setAttribute('role','menu');menu.setAttribute('aria-label','Move to person');
  for(const target of targets){
   const item=doc.createElement('button');item.type='button';item.setAttribute('role','menuitem');item.textContent=target.name;
   item.onclick=()=>{close();choose(target.person_id);};menu.append(item);
  }
  const opened=menu;
  menu.addEventListener('toggle',event=>{if(event.newState==='closed'){opened.remove();if(menu===opened){menu=null;button.setAttribute('aria-expanded','false');}}});
  menu.addEventListener('keydown',event=>{
   const items=[...opened.children],index=items.indexOf(doc.activeElement);
   if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
    event.preventDefault();items[event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
   }
   if(event.key==='Escape'){event.preventDefault();close();button.focus();}
  });
  (button.closest('dialog')||doc.body).append(menu);menu.showPopover();
  const rect=button.getBoundingClientRect(),view=doc.defaultView;
  const below=view.innerHeight-rect.bottom-8,above=rect.top-8;
  menu.style.maxHeight=`${Math.min(320,Math.max(below,above))}px`;
  menu.style.left=`${Math.max(8,Math.min(rect.left,view.innerWidth-menu.offsetWidth-8))}px`;
  menu.style.top=`${below>=Math.min(320,menu.scrollHeight)?rect.bottom+4:Math.max(8,rect.top-menu.offsetHeight-4)}px`;
  button.setAttribute('aria-expanded','true');menu.firstElementChild.focus();
 };
 return {close};
}
