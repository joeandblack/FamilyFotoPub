export function setupPeopleOrder(grid,save,{selector='.named-person-card',idKey='group',enabled=()=>true,dropSelector=null,onDrop=null,scrollElement=null}={}){
 let source=null,target=null,after=false,busy=false,dragging=false,suppress=false;
 const card=node=>node?.closest?.(selector);
 function clear(){for(const node of grid.querySelectorAll('.order-selected,.order-before,.order-after,.order-drop-target'))node.classList.remove('order-selected','order-before','order-after','order-drop-target');}
 function hit(event){
  const node=document.elementFromPoint(event.clientX,event.clientY);
  const external=dropSelector&&onDrop?node?.closest?.(dropSelector):null;
  const candidate=external||card(node);
  return candidate&&candidate!==source?.node&&grid.contains(candidate)?candidate:null;
 }
 grid.addEventListener('pointerdown',event=>{
  if(busy||!enabled()||event.button!==0)return;
  const node=card(event.target);if(!node)return;
  clear();node.classList.add('order-selected');source={node,id:Number(node.dataset[idKey]),x:event.clientX,y:event.clientY,pointer:event.pointerId};target=null;dragging=false;
 });
 grid.addEventListener('dragstart',event=>{if(enabled())event.preventDefault();});
 grid.addEventListener('pointermove',event=>{
  if(!source||event.pointerId!==source.pointer)return;
  if(!dragging&&Math.hypot(event.clientX-source.x,event.clientY-source.y)<6)return;
  if(!dragging){dragging=true;grid.setPointerCapture(event.pointerId);}
  event.preventDefault();clear();source.node.classList.add('order-selected');
  target=hit(event);
  if(target){
   const bounds=target.getBoundingClientRect();after=event.clientX>bounds.left+bounds.width/2;
   target.classList.add(dropSelector&&target.matches(dropSelector)?'order-drop-target':after?'order-after':'order-before');
  }
  const bounds=scrollElement?.getBoundingClientRect()||{top:0,bottom:window.innerHeight};
  if(event.clientY<bounds.top+50)(scrollElement||window).scrollBy(0,-18);else if(event.clientY>bounds.bottom-50)(scrollElement||window).scrollBy(0,18);
 });
 grid.addEventListener('click',event=>{if(suppress){suppress=false;event.preventDefault();event.stopImmediatePropagation();}},true);
 async function finish(event){
  if(!source||event.pointerId!==source.pointer)return;
  const previous=source,drop=event.type==='pointercancel'?null:hit(event),wasDragging=dragging;source=target=null;dragging=false;
  if(grid.hasPointerCapture(event.pointerId))grid.releasePointerCapture(event.pointerId);
  clear();if(!wasDragging)return;suppress=true;setTimeout(()=>suppress=false,0);
  if(!drop||event.type==='pointercancel')return;
  busy=true;grid.setAttribute('aria-busy','true');
  try{
   if(dropSelector&&drop.matches(dropSelector))await onDrop(previous.id,drop);
   else{const bounds=drop.getBoundingClientRect();await save(previous.id,Number(drop.dataset[idKey]),event.clientX>bounds.left+bounds.width/2);}
  }finally{busy=false;grid.setAttribute('aria-busy','false');}
 }
 grid.addEventListener('pointerup',finish);grid.addEventListener('pointercancel',finish);
}
