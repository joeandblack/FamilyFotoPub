export function makeDialogMovable(dialog,handle){
 let drag=null;
 const place=(left,top)=>{
  const r=dialog.getBoundingClientRect();
  dialog.style.margin='0';dialog.style.position='fixed';
  dialog.style.left=Math.max(0,Math.min(left,innerWidth-r.width))+'px';
  dialog.style.top=Math.max(0,Math.min(top,innerHeight-r.height))+'px';
 };
 handle.addEventListener('pointerdown',event=>{
  if(event.button!==0||event.target.closest('button,input,select'))return;
  const r=dialog.getBoundingClientRect();drag={id:event.pointerId,x:event.clientX,y:event.clientY,left:r.left,top:r.top};
  event.preventDefault();handle.setPointerCapture(event.pointerId);
 });
 handle.addEventListener('pointermove',event=>{
  if(drag?.id!==event.pointerId)return;
  place(drag.left+event.clientX-drag.x,drag.top+event.clientY-drag.y);
 });
 for(const event of ['pointerup','pointercancel','lostpointercapture'])handle.addEventListener(event,()=>{drag=null;});
 handle.addEventListener('keydown',event=>{
  const moves={ArrowLeft:[-20,0],ArrowRight:[20,0],ArrowUp:[0,-20],ArrowDown:[0,20]};
  if(event.target!==handle||!moves[event.key])return;
  event.preventDefault();event.stopPropagation();const r=dialog.getBoundingClientRect(),[x,y]=moves[event.key];place(r.left+x,r.top+y);
 });
 const constrain=()=>{if(dialog.open&&dialog.style.left){const r=dialog.getBoundingClientRect();place(r.left,r.top);}};
 new ResizeObserver(constrain).observe(dialog);window.addEventListener('resize',constrain);
 dialog.addEventListener('close',()=>{drag=null;});
}
