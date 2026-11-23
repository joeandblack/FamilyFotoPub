import {backupActive} from './backup-state.mjs';
// Only genuine interaction renews a session, never polling or scan updates.
export function trackSessionActivity(target,send,now=()=>Date.now()){
 let last=-Infinity,pending=false;
 const activity=async event=>{
  if(backupActive(target)||!event.isTrusted||target.visibilityState==='hidden'||pending||now()-last<30000)return;
  pending=true;last=now();
  try{await send();}catch{last=-Infinity;}finally{pending=false;}
 };
 const events=['pointerdown','pointermove','keydown','wheel','touchstart'];
 for(const name of events)target.addEventListener(name,activity,{passive:true,capture:true});
 return ()=>{for(const name of events)target.removeEventListener(name,activity,true);};
}
if(typeof document!=='undefined')trackSessionActivity(document,()=>fetch('/api/auth/activity',{
 method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:'{}'
}));
