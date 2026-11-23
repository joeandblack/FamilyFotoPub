export function createPeopleStartup({request,storage=globalThis.sessionStorage,sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms))}){
 const key='familyfoto-people-startup';let pending={};
 try{pending=JSON.parse(storage?.getItem(key)||'{}');}catch{}
 const save=()=>{try{storage?.setItem(key,JSON.stringify(pending));}catch{}};
 const readyService=async(id,changed)=>{
  if(pending[id])pending[id].message='Starting face detection service...';save();changed();
  let started=false;
  for(let attempt=0;attempt<120;attempt++){
   const response=await request('/api/face-service');
   const service=await response.json().catch(()=>({}));
   if(!response.ok)throw Error(service.detail||'Cannot check face detection service. Try again.');
   if(service.state==='ready')return;
   if(service.state==='stopping')throw Error('Face detection is stopping. Wait for it to stop, then try again.');
   if(!service.busy&&service.state!=='starting'){
    if(started)throw Error(service.message||'Face detection could not start. Check Settings.');
    const result=await request('/api/face-service/start',{method:'POST'});
    if(!result.ok&&result.status!==409){const data=await result.json().catch(()=>({}));throw Error(data.detail||'Face detection could not start. Check Settings.');}
    started=true;
   }
   await sleep(500);
  }
  throw Error('Face detection is still starting. Try Start/Resume again shortly.');
 };
 return {
  merge(status={}){
   const result={...status};
   for(const [id,value] of Object.entries(pending)){
    const live=status[id];
    const expected=value.action==='pause'?['paused','completed','cancelled','blocked']:['running','queued','waiting-for-foreground','waiting-service','awaiting-receipt','completed','blocked','cancelled'];
    if(live&&expected.includes(live.state)){delete pending[id];continue;}
    result[id]={...live,...value};
   }
   save();return result;
  },
  async command(id,action,changed){
   if(pending[id]?.state==='preparing')return;
   pending[id]={state:'preparing',action,message:action==='pause'?'Requesting pause...':'Starting Discover people in the background. It yields to manual actions; you can continue browsing.'};save();changed();
   let submitted=false;
   try{
    let response=await request(`/api/people-preview/jobs/import-${id}`);
    if(response.status===503&&action==='resume'){
     await readyService(id,changed);
     response=await request(`/api/people-preview/jobs/import-${id}`);
    }
    if(!response.ok&&response.status!==404){const data=await response.json().catch(()=>({}));throw Error(data.detail||'Cannot check scan status. Please try again.');}
    if(response.ok&&action==='resume'){
     const live=await response.json();
     if(['running','queued','waiting-for-foreground','awaiting-receipt','completed'].includes(live.state))return;
    }
    const url=response.status===404&&action==='resume'?'/api/people-preview/jobs':`/api/people-preview/jobs/import-${id}/control`;
    const body=response.status===404?{id:`import-${id}`,selection:`import:${id}`}:{action};
    submitted=true;
    const result=await request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!result.ok){
     if(result.status>=500)throw Error('Startup response delayed');
     submitted=false;const data=await result.json().catch(()=>({}));throw Error(data.detail||'Could not start the scan.');
    }
    // Polling may already have replaced the optimistic state while POST was pending.
    if(pending[id])pending[id].message='Request accepted. Checking scan status...';
   }catch(error){
    pending[id]=submitted?{state:'preparing',action,message:'Checking whether Discover people has started. The response is delayed; you can continue browsing. Do not submit again.'}:{state:'start-error',action,message:error.message};
   }
   save();changed();
  }
 };
}
