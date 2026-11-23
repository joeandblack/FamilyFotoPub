const interrupted='Progress connection interrupted. The action may still be running or saved. Refresh People to check the results before trying again.';

// Shared presentation only: callers retain their existing requests and control locks.
export function createPeopleActionProgress(status,{inline=false}={}){
 const bar=document.createElement('progress');bar.className='review-action-progress';bar.hidden=true;
 bar.setAttribute('aria-label','People action progress');
 bar.style.cssText='display:block;width:100%;max-width:320px;height:6px;margin:4px 0;accent-color:#3d795a';
 const region=document.createElement('div');region.className='people-action-feedback';region.style.cssText='flex-basis:100%;min-width:0';
 status.before(region);region.append(status,bar);status.setAttribute('role','status');status.setAttribute('aria-live','polite');
 if(inline){
  region.style.cssText='display:flex;align-items:center;gap:12px;flex:1 1 240px;min-width:0;max-width:560px;margin-inline-start:18px';
  bar.style.cssText='width:120px;max-width:35%;height:6px;margin:0;flex:0 0 120px;accent-color:#3d795a';
 }
 status.style.fontSize='12px';status.style.fontWeight='400';
 let label='Working...';
 const end=(text,error=false)=>{bar.hidden=true;bar.removeAttribute('value');status.classList.toggle('is-error',error);status.style.color=error?'#a12b25':'#68766f';status.textContent=text;};
 return {
  start(text){label=text;end(text);bar.hidden=false;},
  report(event){
   if(event.phase==='processing'&&Number.isFinite(event.total)&&event.total>0&&Number.isFinite(event.done)){
    bar.max=event.total;bar.value=Math.min(event.total,Math.max(0,event.done));
    status.textContent=`${label.replace(/\.\.\.$/,'')} ${bar.value} / ${event.total} faces`;
   }else{bar.removeAttribute('value');status.textContent=event.phase==='saving'?'Saving changes...':event.phase==='refreshing'?'Updating results...':label;}
   bar.hidden=false;
  },
  finish(text=''){end(text);},
  fail(error){end(error.message||String(error),true);}
 };
}

export async function runPeopleReview(endpoint,payload,onProgress=()=>{},{fetcher=fetch,idleMs=40000,path='/review-actions/stream'}={}){
 const controller=new AbortController();let timer,reader;
 const arm=()=>{clearTimeout(timer);timer=setTimeout(()=>controller.abort(),idleMs);};
 try{
  arm();
  const response=await fetcher(`${endpoint}${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal});
  if(!response.ok){const error=Error('People could not start the action. Refresh People and check the selection.');error.reviewFailure=true;throw error;}
  if(!response.body)throw Error(interrupted);
  reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';
  while(true){
   const {value,done}=await reader.read();if(done)throw Error(interrupted);
   arm();buffer+=decoder.decode(value,{stream:true});
   if(buffer.length>16*1024*1024)throw Error(interrupted);
   let newline;
   while((newline=buffer.indexOf('\n'))>=0){
    const line=buffer.slice(0,newline);buffer=buffer.slice(newline+1);if(!line.trim())continue;
    const event=JSON.parse(line);
    if(event.state==='failed'){const error=Error(event.message||'Unable to complete People action');error.reviewFailure=true;throw error;}
    if(event.state==='completed')return event.result;
    if(event.state==='running')onProgress(event);
   }
  }
 }catch(error){throw error.reviewFailure?error:Error(interrupted);}
 finally{clearTimeout(timer);controller.abort();if(reader)await reader.cancel().catch(()=>{});}
}

export function reviewProgressText(event,label){
 const phase={starting:'Starting...',waiting:'Working...',checking:'Working...',updating:'Updating...',saving:'Saving changes...',refreshing:'Updating...'};
 const count=Number.isInteger(event.total)&&event.total>0?` ${event.done||0} / ${event.total} faces`:'';
 return phase[event.phase]||`${label.replace(/\.\.\.$/,'')}${count}...`;
}
