const defaults=()=>({mode:'fill',zoom:1,x:0,y:0});
export function coverFraming(album){
 return album.cover_framing?.media_id===album.cover_id?{...defaults(),...album.cover_framing}:defaults();
}
export function coverGeometry(width,height,settings){
 const ratio=4/3,scale=(settings.mode==='fit'?Math.min:Math.max)(ratio/width,1/height)*settings.zoom;
 const w=width*scale/ratio,h=height*scale;
 return {width:w*100,height:h*100,left:50+Math.abs(w-1)*50*settings.x,top:50+Math.abs(h-1)*50*settings.y};
}
export function paintCover(image,settings){
 if(!image.naturalWidth)return;
 const box=coverGeometry(image.naturalWidth,image.naturalHeight,settings);
 Object.assign(image.style,{inset:'auto',width:`${box.width}%`,height:`${box.height}%`,left:`${box.left}%`,top:`${box.top}%`,maxWidth:'none',objectFit:'fill',transform:'translate(-50%,-50%)'});
}
export function applyCover(image,album){
 const settings=coverFraming(album);
 image.addEventListener('load',()=>{const placeholder=image.parentElement?.querySelector('.album-cover-placeholder');if(placeholder)placeholder.hidden=true;paintCover(image,settings);});
 paintCover(image,settings);
}
export function editCover({album,api,onSaved,endpoint=`/api/albums/${album.id}/cover-framing`}){
 const make=(tag,text)=>{const node=document.createElement(tag);if(text)node.textContent=text;return node;};
 const dialog=make('dialog');dialog.className='album-cover-editor';dialog.setAttribute('aria-label','Adjust album cover');
 const title=make('h2','Adjust cover'),preview=make('div');preview.className='album-cover-preview';preview.tabIndex=0;preview.setAttribute('aria-label','Cover position. Drag or use arrow keys to reposition.');
 const image=make('img');image.alt='Album cover preview';image.draggable=false;preview.append(image);
 let settings=coverFraming(album),busy=false,drag=null;
 const status=make('p','Loading cover...');status.setAttribute('role','status');
 const modes=make('div');modes.className='album-cover-modes';modes.setAttribute('role','group');modes.setAttribute('aria-label','Image fit');
 const button=(label,fn)=>{const b=make('button',label);b.type='button';b.onclick=fn;return b;};
 const fill=button('Fill',()=>{settings.mode='fill';render();}),fit=button('Fit',()=>{settings.mode='fit';render();});modes.append(fill,fit);
 const zoom=make('input');zoom.type='range';zoom.min='1';zoom.max='4';zoom.step='.01';
 const label=make('label','Zoom');label.append(zoom);zoom.oninput=()=>{settings.zoom=Number(zoom.value);render();};
 const reset=button('Reset',()=>{settings=defaults();render();});
 const cancel=button('Cancel',()=>dialog.close()),save=button('Save',async()=>{
  if(busy)return;busy=true;controls.forEach(c=>c.disabled=true);preview.inert=true;status.textContent='Saving cover...';
  try{const result=await api(endpoint,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({...settings,media_id:album.cover_id})});dialog.close();await onSaved(result);}
  catch(error){status.textContent=error.message;}
  finally{busy=false;controls.forEach(c=>c.disabled=false);preview.inert=false;}
 });save.className='album-primary';save.disabled=true;
 const controls=[fill,fit,zoom,reset,cancel,save],actions=make('div');actions.className='album-form-actions';actions.append(reset,cancel,save);
 function render(){zoom.value=settings.zoom;fill.setAttribute('aria-pressed',settings.mode==='fill');fit.setAttribute('aria-pressed',settings.mode==='fit');paintCover(image,settings);}
 const clamp=n=>Math.max(-1,Math.min(1,n));
 preview.onpointerdown=event=>{if(busy||!image.naturalWidth||event.button!==0)return;event.preventDefault();preview.focus();preview.setPointerCapture(event.pointerId);drag={x:event.clientX,y:event.clientY,sx:settings.x,sy:settings.y};};
 preview.onpointermove=event=>{
  if(!drag)return;const rect=preview.getBoundingClientRect(),box=coverGeometry(image.naturalWidth,image.naturalHeight,settings);
  const dx=Math.abs(box.width/100-1)*rect.width/2,dy=Math.abs(box.height/100-1)*rect.height/2;
  settings.x=dx>.1?clamp(drag.sx+(event.clientX-drag.x)/dx):0;settings.y=dy>.1?clamp(drag.sy+(event.clientY-drag.y)/dy):0;render();
 };
 preview.onpointerup=preview.onpointercancel=preview.onlostpointercapture=()=>{drag=null;};
 preview.onkeydown=event=>{if(busy||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();if(event.key==='ArrowLeft')settings.x=clamp(settings.x-.05);if(event.key==='ArrowRight')settings.x=clamp(settings.x+.05);if(event.key==='ArrowUp')settings.y=clamp(settings.y-.05);if(event.key==='ArrowDown')settings.y=clamp(settings.y+.05);render();};
 image.onload=()=>{status.textContent='';save.disabled=false;render();};image.onerror=()=>{status.textContent='Cover preview unavailable. Close and try again.';save.disabled=true;};
 dialog.append(title,preview,modes,label,status,actions);document.body.append(dialog);
 dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});dialog.addEventListener('close',()=>dialog.remove(),{once:true});
 image.src=`/api/media/${album.cover_id}/thumbnail`;render();dialog.showModal();cancel.focus();
}
