import {setupFullscreen} from './video-player.mjs';
// Screen-space pan sits outside the existing crop/rotation transform.
export function setupPhotoZoom(dialog, surface) {
  const bar=document.createElement('div');bar.className='photo-zoom-controls';bar.hidden=true;
  bar.setAttribute('role','group');bar.setAttribute('aria-label','Photo zoom');
  const button=(id,label,icon)=>{
    const b=document.createElement('button');b.id=id;b.type='button';b.title=label;b.setAttribute('aria-label',label);
    b.innerHTML=`<i data-lucide="${icon}" aria-hidden="true"></i>`;bar.append(b);return b;
  };
  const minus=button('photoZoomOut','Zoom out (−)','zoom-out');
  const reset=button('photoZoomFit','Reset photo to fit (0)','rotate-ccw');
  const plus=button('photoZoomIn','Zoom in (+); drag to pan; Shift+arrows to pan','zoom-in');
  const status=document.createElement('span');status.setAttribute('role','status');status.setAttribute('aria-live','polite');bar.append(status);
  const fullscreen=button('photoFullscreen','Fullscreen','maximize');
  let disposeFullscreen=null;
  const fullscreenError=document.createElement('p');fullscreenError.className='viewer-message';fullscreenError.hidden=true;fullscreenError.setAttribute('role','status');
  surface.after(fullscreenError);
  surface.after(bar);
  let frame=null,base='',width=0,height=0,scale=1,x=0,y=0;
  const pointers=new Map();let gesture=null;
  function paint(){
    const bx=Math.max(0,(width*scale-surface.clientWidth)/2),by=Math.max(0,(height*scale-surface.clientHeight)/2);
    x=Math.max(-bx,Math.min(bx,x));y=Math.max(-by,Math.min(by,y));
    if(frame)frame.style.transform=`translate(${x}px,${y}px) scale(${scale}) ${base}`;
    surface.classList.toggle('photo-zoomed',!!frame&&scale>1);
    surface.dataset.zoom=String(scale);minus.disabled=reset.disabled=scale===1;plus.disabled=scale===8;
    status.textContent=`${Math.round(scale*100)}%`;
  }
  function zoom(value){scale=Math.max(1,Math.min(8,value));paint();}
  function clear(){for(const id of pointers.keys())if(surface.hasPointerCapture(id))surface.releasePointerCapture(id);pointers.clear();gesture=null;}
  function fit(){clear();scale=1;x=y=0;paint();}
  plus.onclick=()=>zoom(scale*1.5);minus.onclick=()=>zoom(scale/1.5);reset.onclick=fit;
  function snapshot(){
    const p=[...pointers.values()];gesture=p.length>1?{distance:Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y),scale,x,y,cx:(p[0].x+p[1].x)/2,cy:(p[0].y+p[1].y)/2}:p.length?{x,y,cx:p[0].x,cy:p[0].y}:null;
  }
  surface.addEventListener('pointerdown',e=>{
    if(!frame||scale===1||e.button!==0)return;
    e.preventDefault();surface.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});snapshot();
  });
  surface.addEventListener('pointermove',e=>{
    if(!pointers.has(e.pointerId)||!gesture)return;
    pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});const p=[...pointers.values()];
    if(p.length>1&&gesture.distance){
      scale=Math.max(1,Math.min(8,gesture.scale*Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y)/gesture.distance));
      x=gesture.x+(p[0].x+p[1].x)/2-gesture.cx;y=gesture.y+(p[0].y+p[1].y)/2-gesture.cy;
    }else{x=gesture.x+p[0].x-gesture.cx;y=gesture.y+p[0].y-gesture.cy;}
    paint();
  });
  for(const name of ['pointerup','pointercancel','lostpointercapture'])surface.addEventListener(name,e=>{pointers.delete(e.pointerId);snapshot();});
  surface.addEventListener('dragstart',e=>{if(frame)e.preventDefault();});
  return {
    closeFullscreen(){disposeFullscreen?.();disposeFullscreen=null;fullscreenError.hidden=true;},
    reset(){clear();frame=null;base='';scale=1;x=y=0;bar.hidden=true;paint();},
    layout(element,layout,rotation){
      if(!disposeFullscreen)disposeFullscreen=setupFullscreen(fullscreen,document,message=>{fullscreenError.textContent=message;fullscreenError.hidden=false;},()=>globalThis.lucide?.createIcons(),dialog.querySelector('.viewer-surface'));
      frame=element;base=layout.transform;const odd=rotation%180!==0;width=parseFloat(odd?layout.height:layout.width);height=parseFloat(odd?layout.width:layout.height);bar.hidden=false;paint();
    },
    key(event){
      if(!frame||bar.hidden||dialog.classList.contains('cropping')||event.ctrlKey||event.metaKey||event.altKey||event.target.closest('input,textarea,select,video,[contenteditable]:not([contenteditable="false"])'))return false;
      if(['+','=','-','0'].includes(event.key)){event.preventDefault();event.key==='0'?fit():zoom(scale*(event.key==='-'?1/1.5:1.5));return true;}
      if(scale>1&&event.shiftKey&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();x+=event.key==='ArrowLeft'?60:event.key==='ArrowRight'?-60:0;y+=event.key==='ArrowUp'?60:event.key==='ArrowDown'?-60:0;paint();return true;}
      return false;
    }
  };
}
