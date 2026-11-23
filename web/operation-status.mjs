export const peopleActiveStates=['queued','running','waiting-for-foreground','awaiting-receipt'];
export function operationStatus(data,{peopleReady=true}={}){
  const operations=[
    ['duplicates','Scan dups',data.duplicates],
    ['conversion','Convert videos',data.conversion],
    ['metadata','Process videos',data.video],
    ['import','Importing',data.job],
    ...Object.values(data.people||{}).map(status=>['people','Discover people',status]),
  ];
  const selected=operations.find(([stage, ,status])=>(stage==='people'?peopleActiveStates:['running','scanning']).includes(status?.state))
    ||operations.find(([, ,status])=>['paused-error','blocked','failed','error'].includes(status?.state))
    ||operations.find(([, ,status])=>status?.state==='waiting-service');
  return selected?{stage:selected[0],label:selected[1],status:selected[2]}:{stage:null,label:peopleReady?'Ready':'Initializing...',status:null};
}

export function operationIndicator(operation,monitor,now=Date.now()){
 const active=operation.status;
 if(!active)return {state:operation.label==='Ready'?'ready':'idle',label:operation.label,title:operation.label==='Ready'?'FamilyFoto is ready. No active processing.':'Checking operation status...'};
 if(['paused-error','blocked','failed','error'].includes(active.state))return {state:'error',label:'Stopped with error',title:`${operation.label}: ${active.error||active.state}`};
 if(active.state==='waiting-service')return {state:'waiting',label:'Waiting for service',title:`${operation.label}: waiting for its required service. Check Imports for details.`};
 if(operation.stage==='people')return {state:'running',label:operation.label,title:`${operation.label}: ${active.state} (${active.done??0}/${active.total??0} photos)`};
 const checked=Date.parse(monitor?.updated);
 if(monitor?.state!=='healthy'||!Number.isFinite(checked)||now-checked>60000)return {state:'error',label:'Check status',title:`${operation.label}: health monitoring is unavailable or out of date. Check Imports.`};
 return {state:'running',label:operation.label,title:operation.label};
}
