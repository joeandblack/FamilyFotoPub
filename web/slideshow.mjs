export function createSlideshow({advance,changed,error,schedule=setTimeout,cancel=clearTimeout}){
  let active=false,seconds=3,timer=null,media=null,detach=()=>{},revision=0;
  function clear(){if(timer!==null)cancel(timer);timer=null;}
  function stop(){active=false;clear();changed(false);}
  function next(){clear();if(active)advance();}
  function arm(){
    clear();
    if(active&&media?.tagName==='IMG'&&media.complete&&media.naturalWidth)
      timer=schedule(next,seconds*1000);
  }
  function play(){
    if(!media)return;
    active=true;changed(true);
    if(['VIDEO','AUDIO'].includes(media.tagName)){
      const ticket=revision;
      if(media.ended)media.currentTime=0;
      media.play().catch(()=>{
        if(ticket!==revision||!active)return;
        stop();error('Playback could not start. Press Play to try again.');
      });
    }else arm();
  }
  function release(){clear();revision++;detach();detach=()=>{};media=null;}
  return {
    get active(){return active;},
    stop,
    toggle(){if(active){stop();['VIDEO','AUDIO'].includes(media?.tagName)&&media.pause();}else play();},
    interval(value){seconds=value;arm();},
    release,
    reset(){stop();release();},
    attach(element){
      release();media=element;
      const failed=()=>{stop();error('Preview unavailable. Autoplay stopped.');};
      const paused=()=>{if(!element.ended)stop();};
      element.addEventListener('load',arm);
      element.addEventListener('ended',next);
      element.addEventListener('error',failed);
      element.addEventListener('pause',paused);
      detach=()=>{
        element.removeEventListener('load',arm);element.removeEventListener('ended',next);
        element.removeEventListener('error',failed);element.removeEventListener('pause',paused);
      };
      if(active)play();
    }
  };
}
