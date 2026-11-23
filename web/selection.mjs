export function rectangle(a,b){return {left:Math.min(a.x,b.x),top:Math.min(a.y,b.y),right:Math.max(a.x,b.x),bottom:Math.max(a.y,b.y)};}
export function intersects(a,b){return a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;}
export function contentPoint(client,viewport,scroll){return {x:client.x-viewport.left+scroll.left,y:client.y-viewport.top+scroll.top};}
export function dragSelection(base,hits,mode){
 const next=mode==='replace'?new Set():new Set(base);
 for(const id of hits){if(mode==='toggle'&&base.has(id))next.delete(id);else next.add(id);}return next;
}
// What a drag does to the selection. `plain` is for a drag with no modifier key: a gallery
// where a click replaces the selection replaces, one where a click adds to it must add,
// or a click made with an unsteady hand would wipe what was already chosen.
export function dragMode(event,plain='replace'){return event.ctrlKey||event.metaKey?'toggle':event.shiftKey?'add':plain;}
export function rangeSelection(ids,anchor,id){const a=ids.indexOf(anchor),b=ids.indexOf(id);return a<0||b<0?[id]:ids.slice(Math.min(a,b),Math.max(a,b)+1);}
export function downloadLinks(rows,selected){
 const unique=[...new Map(rows.filter(m=>selected.has(m.id)).map(m=>[m.id,m])).values()];
 return unique.map((m,i)=>({id:m.id,name:m.name,url:`/api/media/${m.id}/download${unique.length>1?'?disambiguate=true':''}`}));
}

// Geometry is in scroll-content coordinates; the visible rectangle is clipped
// to the gallery. Touch pointers keep their native scrolling behavior.
export function setupDragSelection(grid,{enabled,selected,change,overlayParent=document.body,scrollContainer=()=>grid,plain='replace'}){
 let drag=null,frame=0,suppress=false;
 const box=document.createElement('div');box.className='selection-rectangle';box.hidden=true;overlayParent.append(box);
 const point=e=>contentPoint({x:e.clientX,y:e.clientY},grid.getBoundingClientRect(),{left:grid.scrollLeft,top:grid.scrollTop});
 function update(){
  if(!drag?.active)return;
  const bounds=grid.getBoundingClientRect();
  const end=point({clientX:Math.max(bounds.left,Math.min(bounds.right,drag.x)),clientY:Math.max(bounds.top,Math.min(bounds.bottom,drag.y))});
  const area=rectangle(drag.start,end),hits=[];
  for(const tile of grid.querySelectorAll('[data-id]')){
   const r=tile.getBoundingClientRect(),p=point({clientX:r.left,clientY:r.top});
   if(intersects(area,{left:p.x,top:p.y,right:p.x+r.width,bottom:p.y+r.height}))hits.push(Number(tile.dataset.id));
  }
  change(dragSelection(drag.base,hits,drag.mode));
  const left=Math.max(bounds.left,area.left+bounds.left-grid.scrollLeft),top=Math.max(bounds.top,area.top+bounds.top-grid.scrollTop);
  const right=Math.min(bounds.right,area.right+bounds.left-grid.scrollLeft),bottom=Math.min(bounds.bottom,area.bottom+bounds.top-grid.scrollTop);
  Object.assign(box.style,{left:left+'px',top:top+'px',width:Math.max(0,right-left)+'px',height:Math.max(0,bottom-top)+'px'});box.hidden=false;
 }
 function scroll(){
  if(!drag?.active)return;
  const scroller=scrollContainer(),r=scroller.getBoundingClientRect(),delta=drag.y<r.top+28?-14:drag.y>r.bottom-28?14:0;
  if(delta){scroller.scrollTop+=delta;update();}frame=requestAnimationFrame(scroll);
 }
 function cancel(){if(drag?.active)suppress=true;drag=null;cancelAnimationFrame(frame);box.hidden=true;setTimeout(()=>suppress=false,0);}
 grid.addEventListener('pointerdown',e=>{
  if(!enabled()||e.pointerType==='touch'||e.button!==0)return;
  // Safari can start a native image/text range from its previous selection
  // anchor while our marquee is active. Keep mouse selection entirely ours;
  // touch pointers above still retain native scrolling.
  e.preventDefault();window.getSelection()?.removeAllRanges();
  e.target.closest('[data-id]')?.focus({preventScroll:true});
  drag={start:point(e),x:e.clientX,y:e.clientY,downX:e.clientX,downY:e.clientY,base:new Set(selected()),mode:dragMode(e,plain),active:false,id:e.pointerId};
 });
 grid.addEventListener('pointermove',e=>{
  if(!drag||e.pointerId!==drag.id)return;
  drag.x=e.clientX;drag.y=e.clientY;
  if(!drag.active&&Math.hypot(e.clientX-drag.downX,e.clientY-drag.downY)>4){drag.active=true;grid.setPointerCapture(e.pointerId);frame=requestAnimationFrame(scroll);}
  if(drag.active){e.preventDefault();update();}
 });
 grid.addEventListener('pointerup',cancel);grid.addEventListener('pointercancel',cancel);grid.addEventListener('lostpointercapture',cancel);
 grid.addEventListener('scroll',update);
 grid.addEventListener('dragstart',e=>{if(enabled())e.preventDefault();});
 grid.addEventListener('click',e=>{if(suppress){suppress=false;e.preventDefault();e.stopImmediatePropagation();}},true);
 return {cancel};
}
