import {photoLayout,setupCrop} from './crop.mjs';
import {confirmAction} from './confirm-dialog.mjs';
import {deleteWithProgress} from './deletion-progress.mjs';
import {setupPhotoZoom} from './photo-zoom.mjs';
import {setupVideoPlayer} from './video-player.mjs';
import {isAudio} from './audio-media.mjs';
import {createSlideshow} from './slideshow.mjs';
import {createPhotoPreloader,mediaURL} from './photo-preload.mjs';
import {confirmAlbumTrash} from './album-trash-warning.mjs';
export function setupViewer({api,context,fields,size,changed,addToAlbum}) {
  const $=id=>document.getElementById(id);
  const dialog=$('viewer');
  const surface=document.createElement('div');surface.className='viewer-surface';
  surface.append(...dialog.childNodes);dialog.append(surface);
  const zoom=setupPhotoZoom(dialog,$('fullMedia'));
  const download=document.createElement('button');download.id='viewerDownload';
  download.title='Download original photo';download.setAttribute('aria-label',download.title);
  download.innerHTML='<i data-lucide="download"></i>';
  $('viewerInfo').before(download);
  const albumButton=document.createElement('button');albumButton.id='viewerAlbum';albumButton.className='album-write';albumButton.title='Add to album';albumButton.setAttribute('aria-label','Add to album');albumButton.innerHTML='<i data-lucide="folder-plus" aria-hidden="true"></i>';albumButton.onclick=()=>{if(!busy&&list[index])addToAlbum?.(list[index].id);};$('viewerInfo').before(albumButton);
  download.onclick=()=>{
    const m=list[index];if(busy||!m||m.deleting_at)return;
    const link=document.createElement('a');link.href=`/api/media/${m.id}/download`;
    link.download='';document.body.append(link);link.click();link.remove();
  };
  let list=[],index=0,page=1,pages=1,filters={},busy=false,generation=0,disposeVideo=()=>{};
  const preloader=createPhotoPreloader();let pageAhead=null,preparing=false;
  function nextPage(target){
    if(pageAhead?.page!==target){
      const promise=api('/api/media?'+new URLSearchParams({...filters,page:target}));promise.catch(()=>{});
      pageAhead={page:target,promise};
    }
    return pageAhead.promise;
  }
  async function warm(){
    if(!slideshow.active)return;
    const ticket=generation,upcoming=list.slice(index+1,index+3);
    preloader.warm(upcoming);
    if(upcoming.length<2&&page<pages){
      try{const result=await nextPage(page+1);if(ticket===generation&&slideshow.active)preloader.warm([...upcoming,...result.items].slice(0,2));}catch{}
    }
  }
  function showError(message){$('captureTime').textContent=message;$('captureTime').dataset.error='true';}
  const autoplay=document.createElement('button');autoplay.id='viewerAutoplay';
  const interval=document.createElement('select');interval.className='viewer-interval';
  interval.title='Photo display time';interval.setAttribute('aria-label',interval.title);
  for(const seconds of [3,5,10,15,30]){const option=document.createElement('option');option.value=seconds;option.textContent=`${seconds}s`;interval.append(option);}
  interval.value='3';
  const playback=document.createElement('div');playback.className='viewer-playback';playback.append(autoplay,interval);
  dialog.querySelector('.viewer-navigation').append(playback,dialog.querySelector('.photo-zoom-controls'));
  function autoplayState(active){
    autoplay.title=active?'Pause autoplay':'Play slideshow';autoplay.setAttribute('aria-label',autoplay.title);
    autoplay.setAttribute('aria-pressed',String(active));
    autoplay.innerHTML=`<i data-lucide="${active?'pause':'play'}"></i>`;globalThis.lucide?.createIcons();
  }
  const slideshow=createSlideshow({changed:autoplayState,error:showError,advance:()=>{
    if(busy||crop.active||!dialog.open){slideshow.stop();return;}
    if(index===list.length-1&&page>=pages){slideshow.stop();return;}
    move(1,true);
  }});
  autoplayState(false);
  autoplay.onclick=()=>{
    if(preparing){generation++;preparing=false;busy=false;slideshow.stop();controls();return;}
    if(!busy&&!crop.active){slideshow.toggle();if(slideshow.active)warm();}
  };
  interval.onchange=()=>slideshow.interval(Number(interval.value));
  const crop=setupCrop({api,current:()=>list[index],finish:()=>{fit();changed();},error:showError});
  $('viewerCrop').onclick=()=>{if(!busy){slideshow.stop();info(false);zoom.reset();fit();crop.start();}};
  function controls(){
    autoplay.disabled=busy&&!preparing;
    for(const id of ['viewerTrash','viewerRestore','viewerDelete','viewerRotate','viewerCrop','viewerDownload','viewerAlbum'])$(id).disabled=busy;
    $('viewerPrevious').disabled=busy||(index===0&&page===1);
    $('viewerNext').disabled=busy||(index===list.length-1&&page===pages);
  }
  function info(open){
    $('viewerDetails').hidden=!open;
    $('viewerInfo').setAttribute('aria-expanded',String(open));
  }
  function render(prepared=null){
    zoom.reset();
    slideshow.release();
    disposeVideo();disposeVideo=()=>{};
    const m=list[index],v=m.video_metadata||{};
    if(m.kind==='video')zoom.closeFullscreen();
    $('viewerRotate').hidden=isAudio(m)||Boolean(m.deleting_at);
    $('viewerCrop').hidden=m.kind!=='image'||Boolean(m.deleting_at);
    download.hidden=Boolean(m.deleting_at);
    albumButton.hidden=!addToAlbum||filters.kind==='trash'||Boolean(m.deleting_at);
    albumButton.disabled=busy;
    download.title=isAudio(m)?'Download audio':m.kind==='video'?'Download video':'Download original photo';
    download.setAttribute('aria-label',download.title);
    $('viewerTrash').hidden=filters.kind==='trash';
    $('viewerTrash').title=filters.album?'Remove from this album':'Move to Trash';
    $('viewerTrash').setAttribute('aria-label',$('viewerTrash').title);
    $('viewerTrash').innerHTML=`<i data-lucide="${filters.album?'folder-minus':'trash-2'}"></i>`;
    globalThis.lucide?.createIcons();
    $('viewerRestore').hidden=filters.kind!=='trash'||Boolean(m.deleting_at);
    $('viewerDelete').hidden=filters.kind!=='trash';
    const media=prepared||document.createElement(isAudio(m)?'audio':m.kind==='video'?'video':'img');
    if(m.kind==='video'){media.autoplay=true;media.playsInline=true;}
    else media.alt=m.name;
    const message=document.createElement('p');
    message.className='viewer-message';message.textContent='Loading...';message.setAttribute('role','status');
    message.hidden=Boolean(prepared);
    media.addEventListener(m.kind==='video'?'loadeddata':'load',()=>{if(!media.isConnected)return;message.hidden=true;fit();});
    media.addEventListener('error',()=>{if(!media.isConnected)return;zoom.reset();message.hidden=false;message.textContent='Preview unavailable';});
    if(!prepared)media.src=mediaURL(m);
    if(m.kind==='image'){
      const viewport=document.createElement('div');viewport.className='photo-viewport';viewport.append(media);
      $('fullMedia').replaceChildren(viewport,message);
    }else if(isAudio(m)){
      media.controls=true;
      const panel=document.createElement('div');panel.className='audio-viewer';
      const title=document.createElement('p');title.textContent=m.name;
      const icon=document.createElement('i');icon.setAttribute('data-lucide','music-2');icon.setAttribute('aria-hidden','true');
      panel.append(icon,title,media);$('fullMedia').replaceChildren(panel,message);globalThis.lucide?.createIcons();
      disposeVideo=()=>{media.pause();media.removeAttribute('src');media.load();};
    }else {
      $('fullMedia').replaceChildren(media,message);
      disposeVideo=setupVideoPlayer(media,dialog,$('viewerInfo'));
    }
    fit();
    delete $('captureTime').dataset.error;
    $('captureTime').textContent='';
    fields('mediaDetails',{...(m.trashed_at?{'Moved to trash':m.trashed_at,'Deletion started':m.deleting_at||'No'}:{}),'Filename':m.name,'Capture time':m.captured_at?m.captured_at.replace('T',' '):'Unknown','Dimensions':m.width?`${m.width} x ${m.height}`:'Unknown',
      'Size':size(m.size),'Original source path':m.source_path,'Stored relative path':m.relative_path,
      'Stored SHA-256':m.stored_sha256||m.sha256,'Source SHA-256':m.sha256,
      ...(m.conversion_duplicate_of?{'Identical converted output':`Retained media #${m.conversion_duplicate_of}; this original AVI is preserved. Restoring it will not reconvert it.`}:{}),
      'Metadata warning':m.metadata_error||m.video_error||'None',
      ...(m.kind==='video'?{'Video conversion':m.conversion_state||'Not converted',
        'Conversion warning':m.conversion_error||'None','Duration':v.duration_seconds!=null?v.duration_seconds.toFixed(2)+' seconds':'Unknown',
        'Codecs':[v.video_codec,v.audio_codec].filter(Boolean).join(' / ')||'Unknown',
        'Playback':v.browser_compatibility||'Not yet assessed','Date tag':v.capture_time_raw,
        'Date provenance':v.capture_time_provenance,'Date timezone':v.capture_time_timezone,'Date caveat':v.capture_time_caveat}:{})});
    controls();
    slideshow.attach(media);
    if(!slideshow.active)preloader.warm([]);
    warm();
  }
  async function move(direction,automatic=false){
    if(busy||crop.active||!dialog.open)return;
    const next=index+direction,local=next>=0&&next<list.length,target=local?page:page+direction;
    if(target<1||target>pages)return;
    busy=true;preparing=automatic;controls();const ticket=++generation;
    try{
      const result=local?{items:list,page,pages}:await nextPage(target);
      if(ticket!==generation||!dialog.open)return;
      if(result.items.length){
        const selected=local?next:direction>0?0:result.items.length-1;
        const prepared=automatic?await preloader.load(result.items[selected]):null;
        if(ticket!==generation||!dialog.open)return;
        list=result.items;page=result.page;pages=result.pages;index=selected;render(prepared);
      }
      else slideshow.stop();
    }catch(error){if(ticket===generation){slideshow.stop();showError(error.message);}}
    finally{if(ticket===generation){busy=false;preparing=false;controls();}}
  }
  $('viewerPrevious').onclick=()=>move(-1);
  $('viewerNext').onclick=()=>move(1);
  $('viewerInfo').onclick=()=>info($('viewerDetails').hidden);
  function fit(){
    const video=$('fullMedia').querySelector('video');
    if(video&&video.videoWidth){
      const frame=$('fullMedia');
      const layout=photoLayout(video.videoWidth,video.videoHeight,frame.clientWidth,frame.clientHeight,null,list[index].rotation||0);
      Object.assign(video.style,layout.frame);return;
    }
    const image=$('fullMedia').querySelector('img');
    if(!image||!list[index]||!image.naturalWidth)return;
    const frame=$('fullMedia');
    const layout=photoLayout(image.naturalWidth,image.naturalHeight,frame.clientWidth,frame.clientHeight,list[index].crop,list[index].rotation||0);
    Object.assign(image.parentElement.style,layout.frame);Object.assign(image.style,layout.image);
    zoom.layout(image.parentElement,layout.frame,list[index].rotation||0);
  }
  new ResizeObserver(fit).observe($('fullMedia'));
  $('viewerRotate').onclick=async()=>{
    if(busy)return;
    slideshow.stop();
    busy=true;controls();const ticket=generation,m=list[index];
    try{
      const result=await api(`/api/media/${m.id}/rotate`,{method:'POST'});
      m.rotation=result.rotation;
      if(ticket===generation&&dialog.open)fit();
      await changed();
    }catch(error){if(ticket===generation)showError(error.message);}
    finally{if(ticket===generation){busy=false;controls();}}
  };
  async function change(action){
    if(busy)return;
    slideshow.stop();
    const m=list[index];
    const confirmationGeneration=generation;
    if(action==='delete'&&!await confirmAction('Permanently delete this destination copy? This cannot be undone. Source files are not affected.',{title:'Permanently delete this item?',confirmLabel:'Delete permanently',danger:true}))return;
    if(busy||confirmationGeneration!==generation)return;
    busy=true;controls();const ticket=generation;
    try{
      if(action==='trash'&&filters.album){
        if(ticket!==generation)return;
        await api(`/api/albums/${filters.album}/remove`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({media_ids:[m.id]})});
        if(ticket===generation){close();await changed();}
        return;
      }
      if(action==='trash'&&!await confirmAlbumTrash(api,[m.id]))return;
      if(ticket!==generation)return;
      if(action==='delete'){
        const outcome=await deleteWithProgress({ids:[m.id],api,elements:()=>[...$('fullMedia').children],refresh:changed,keepGoing:()=>ticket===generation});
        if(outcome.deleted&&ticket===generation)close();
        return;
      }
      await api(`/api/media/${m.id}/trash/${action}`,{method:'POST'});
      if(ticket!==generation)return;
      close();await changed();
    }catch(error){if(ticket===generation)showError(error.message);}
    finally{if(ticket===generation){busy=false;controls();}}
  }
  $('viewerTrash').onclick=()=>change('trash');
  $('viewerRestore').onclick=()=>change('restore');
  $('viewerDelete').onclick=()=>change('delete');
  function cleanup(){slideshow.reset();preloader.clear();pageAhead=null;preparing=false;zoom.closeFullscreen();zoom.reset();disposeVideo();disposeVideo=()=>{};crop.stop();generation++;busy=false;$('fullMedia').innerHTML='';info(false);}
  function close(){if(crop.saving)return;cleanup();dialog.close();}
  $('closeViewer').onclick=close;
  dialog.addEventListener('cancel',event=>{event.preventDefault();if(crop.active){if(!crop.saving){crop.stop();fit();}}else close();});
  dialog.addEventListener('close',()=>{if(!dialog.open)cleanup();});
  dialog.addEventListener('keydown',event=>{
    if(zoom.key(event))return;
    if(event.ctrlKey||event.metaKey||event.altKey||event.target.closest('video,input,textarea,select,[contenteditable]:not([contenteditable="false"])'))return;
    if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();move(event.key==='ArrowLeft'?-1:1);}
  });
  return (id,selectedContext)=>{
    slideshow.reset();
    preloader.clear();pageAhead=null;preparing=false;
    const current=selectedContext||context();list=[...current.items];index=list.findIndex(m=>m.id===id);
    if(index<0)return;
    generation++;busy=false;page=current.page;pages=current.pages;filters=current.filters;
    info(false);render();dialog.showModal();dialog.scrollTop=0;
  };
}
