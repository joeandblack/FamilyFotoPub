export function createLoadingStatus(element,{schedule=setTimeout,cancel=clearTimeout}={}){
 let generation=0,timer;
 const reset=()=>{generation++;cancel(timer);element.hidden=true;element.dataset.state='idle';element.textContent='';};
 return {reset,start(label){
  reset();const ticket=generation;element.hidden=false;element.dataset.state='loading';element.textContent=label;
  timer=schedule(()=>{if(ticket===generation)element.textContent=`${label} Still working; this is taking longer than usual.`;},12000);
  return error=>{if(ticket!==generation)return;generation++;cancel(timer);
   element.dataset.state=error?'error':'idle';element.hidden=!error;element.textContent=error?`Unable to load: ${error}. Try Refresh.`:'';
  };
 }};
}
