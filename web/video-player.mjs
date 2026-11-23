export function playbackTime(seconds){
  if(!Number.isFinite(seconds)||seconds<0)return '0:00';
  const n=Math.floor(seconds),h=Math.floor(n/3600),m=Math.floor(n/60)%60,s=String(n%60).padStart(2,'0');
  return h?`${h}:${String(m).padStart(2,'0')}:${s}`:`${m}:${s}`;
}
export function setupFullscreen(button,doc,onError,icons=()=>{},target){
  // Fullscreen the viewer's inner surface, never the dialog or the page root.
  let owned=false,disposed=false;
  const update=()=>{
    const active=Boolean(doc.fullscreenElement);
    if(!active)owned=false;
    const label=active?'Exit fullscreen':'Fullscreen';
    button.title=label;button.setAttribute('aria-label',label);
    button.innerHTML=`<i data-lucide="${active?'minimize':'maximize'}"></i>`;icons();
  };
  button.hidden=!target.requestFullscreen||doc.fullscreenEnabled===false;
  button.onclick=async()=>{
    try{
      if(doc.fullscreenElement)await doc.exitFullscreen();
      else{
        await target.requestFullscreen();owned=true;
        if(disposed&&doc.fullscreenElement===target)await doc.exitFullscreen();
      }
    }catch(error){if(!disposed)onError('Fullscreen unavailable: '+error.message);}
  };
  doc.addEventListener('fullscreenchange',update);update();
  return ()=>{
    disposed=true;doc.removeEventListener('fullscreenchange',update);
    button.onclick=null;
    if(owned&&doc.fullscreenElement===target)doc.exitFullscreen().catch(()=>{});
  };
}

export function setupVideoPlayer(video,dialog,before){
  const surface=dialog.querySelector('.viewer-surface');
  const bar=document.createElement('div');bar.className='video-controls';
  bar.innerHTML='<button title="Play" aria-label="Play"><i data-lucide="play"></i></button><input type="range" min="0" max="1" step="0.1" value="0" aria-label="Video position"><span></span><button title="Fullscreen" aria-label="Fullscreen"><i data-lucide="maximize"></i></button>';
  const volume=document.createElement('div');volume.className='video-volume';
  volume.innerHTML='<button title="Volume" aria-label="Volume" aria-expanded="false" aria-controls="videoVolumePanel"><i data-lucide="volume-2"></i></button><div id="videoVolumePanel" hidden><input type="range" min="0" max="1" step="0.01" value="1" aria-label="Volume level"></div>';
  before.before(volume);surface.append(bar);
  const [play,fullscreen]=bar.querySelectorAll('button'),seek=bar.querySelector('input'),time=bar.querySelector('span');
  const toggle=volume.querySelector('button'),panel=volume.querySelector('div'),slider=volume.querySelector('input');
  function icons(){globalThis.lucide?.createIcons();}
  function show(open){panel.hidden=!open;toggle.setAttribute('aria-expanded',String(open));}
  toggle.onclick=()=>show(true);
  volume.onmouseenter=()=>show(true);
  volume.onmouseleave=()=>{if(!volume.contains(document.activeElement))show(false);};
  volume.onfocusout=event=>{if(!volume.contains(event.relatedTarget))show(false);};
  volume.onkeydown=event=>{if(event.key==='Escape'&&!panel.hidden){event.stopPropagation();show(false);toggle.focus();}};
  const outside=event=>{if(!volume.contains(event.target))show(false);};
  dialog.addEventListener('pointerdown',outside);
  slider.oninput=()=>{video.volume=Number(slider.value);video.muted=video.volume===0;};
  const volumeChanged=()=>{
    slider.value=video.muted?0:video.volume;
    toggle.innerHTML=`<i data-lucide="${video.muted||video.volume===0?'volume-x':'volume-2'}"></i>`;icons();
  };
  function update(){
    const valid=Number.isFinite(video.duration)&&video.duration>0;
    seek.disabled=!valid;seek.max=valid?video.duration:1;seek.value=video.currentTime||0;
    time.textContent=`${playbackTime(video.currentTime)} / ${playbackTime(video.duration)}`;
  }
  function state(){
    const label=video.paused?'Play':'Pause';play.title=label;play.setAttribute('aria-label',label);
    play.innerHTML=`<i data-lucide="${video.paused?'play':'pause'}"></i>`;icons();
  }
  play.onclick=()=>{if(video.paused)video.play().catch(()=>state());else video.pause();};
  seek.oninput=()=>{if(Number.isFinite(video.duration))video.currentTime=Number(seek.value);};
  const fullscreenError=document.createElement('p');fullscreenError.className='viewer-message';fullscreenError.setAttribute('role','status');fullscreenError.hidden=true;surface.append(fullscreenError);
  const disposeFullscreen=setupFullscreen(fullscreen,document,message=>{fullscreenError.textContent=message;fullscreenError.hidden=false;},icons,surface);
  for(const name of ['timeupdate','durationchange','loadedmetadata'])video.addEventListener(name,update);
  for(const name of ['play','pause','ended'])video.addEventListener(name,state);
  video.addEventListener('volumechange',volumeChanged);
  dialog.classList.add('playing-video');update();state();volumeChanged();
  return ()=>{
    video.pause();
    disposeFullscreen();fullscreenError.remove();
    for(const name of ['timeupdate','durationchange','loadedmetadata'])video.removeEventListener(name,update);
    for(const name of ['play','pause','ended'])video.removeEventListener(name,state);
    video.removeEventListener('volumechange',volumeChanged);dialog.removeEventListener('pointerdown',outside);
    bar.remove();volume.remove();dialog.classList.remove('playing-video');
  };
}
