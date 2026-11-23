export const fullCrop={x:0,y:0,width:1,height:1};
export function rotateCrop(rect,degrees){
  let {x,y,width,height}=rect;
  for(let turns=((degrees%360)+360)%360/90;turns>0;turns--){
    [x,y,width,height]=[1-y-height,x,height,width];
  }
  return {x:Math.max(0,x),y:Math.max(0,y),width,height};
}
export function photoLayout(iw,ih,fw,fh,crop,rotation){
  const c=crop||fullCrop,cw=iw*c.width,ch=ih*c.height;
  const odd=rotation%180!==0,scale=Math.min(fw/(odd?ch:cw),fh/(odd?cw:ch));
  return {frame:{width:cw*scale+'px',height:ch*scale+'px',transform:`rotate(${rotation}deg)`},
    image:{width:iw*scale+'px',height:ih*scale+'px',left:-iw*c.x*scale+'px',top:-ih*c.y*scale+'px'}};
}
export function setupCrop({api,current,finish,error}){
  const $=id=>document.getElementById(id);
  let cropper=null,editing=false,saving=false,ready=false,generation=0;
  function stop(){
    generation++;cropper?.destroy();cropper=null;editing=false;ready=false;
    $('cropImage').removeAttribute('src');$('viewer').classList.remove('cropping');
    $('cropEditor').hidden=$('cropActions').hidden=true;
  }
  function start(){
    if(editing||saving)return;
    editing=true;const ticket=++generation,m=current();
    $('viewer').classList.add('cropping');$('cropEditor').hidden=$('cropActions').hidden=false;
    $('cropSave').disabled=$('cropReset').disabled=true;
    const image=$('cropImage');image.src=`/api/media/${m.id}/original`;
    image.onerror=()=>{if(ticket===generation){stop();error('Preview unavailable for cropping');}};
    cropper=new Cropper(image,{viewMode:1,autoCropArea:1,background:false,checkOrientation:false,
      movable:false,zoomable:false,scalable:false,rotatable:true,toggleDragModeOnDblclick:false,
      ready(){
        if(ticket!==generation)return;
        cropper.clear();
        cropper.rotateTo(m.rotation||0);
        const canvas=cropper.getCanvasData(),c=rotateCrop(m.crop||fullCrop,m.rotation||0);
        const container=cropper.getContainerData();
        const scale=Math.min(container.width/canvas.naturalWidth,container.height/canvas.naturalHeight);
        cropper.crop();cropper.setCropBoxData({width:1,height:1});
        cropper.setCanvasData({left:(container.width-canvas.naturalWidth*scale)/2,top:(container.height-canvas.naturalHeight*scale)/2,width:canvas.naturalWidth*scale,height:canvas.naturalHeight*scale});
        cropper.setData({x:c.x*canvas.naturalWidth,y:c.y*canvas.naturalHeight,width:c.width*canvas.naturalWidth,height:c.height*canvas.naturalHeight});
        ready=true;$('cropSave').disabled=$('cropReset').disabled=false;
      }});
  }
  async function save(reset=false){
    if(!ready||saving)return;
    const m=current(),ticket=generation;
    let crop=null;
    if(!reset){
      const d=cropper.getData(),canvas=cropper.getCanvasData();
      const x=Math.max(0,d.x/canvas.naturalWidth),y=Math.max(0,d.y/canvas.naturalHeight);
      crop=rotateCrop({x,y,width:Math.min(1-x,d.width/canvas.naturalWidth),height:Math.min(1-y,d.height/canvas.naturalHeight)},-(m.rotation||0));
    }
    saving=true;$('cropSave').disabled=$('cropReset').disabled=$('cropCancel').disabled=true;
    try{
      const result=await api(`/api/media/${m.id}/crop`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(crop)});
      m.crop=result.crop;
      if(ticket===generation){stop();finish();}
    }catch(e){if(ticket===generation)error(e.message);}
    finally{saving=false;$('cropCancel').disabled=false;if(ticket===generation)$('cropSave').disabled=$('cropReset').disabled=false;}
  }
  $('cropSave').onclick=()=>save();$('cropReset').onclick=()=>save(true);
  $('cropCancel').onclick=()=>{if(!saving){stop();finish();}};
  return {start,stop,get active(){return editing;},get saving(){return saving;}};
}
