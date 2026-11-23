import {size} from './size.mjs';

export function uploadBytes(session){
 const finite=value=>Number.isFinite(Number(value))?Math.max(0,Number(value)):0;
 const total=finite(session.total_bytes);
 const received=Math.min(total,finite(session.received??session.files?.filter(f=>f.disposition==='accepted').reduce((n,f)=>n+finite(f.received),0)));
 // Never round an incomplete transfer up to 100%. Empty transfers have no
 // byte work, but still require verification and a durable server handoff.
 const percent=total?Math.floor(received/total*1000)/10:100;
 return {received,total,percent,text:`${percent.toFixed(1)}% transferred · ${size(received)} / ${size(total)}${total?'':' (no bytes to transfer)'}`};
}

// One selected upload owns the helper. Selection epochs and request tickets
// prevent delayed requests from replacing a newer selection or fresher status.
export function uploadMessage(session){
 const name=`Upload · ${session.label}`,job=session.job_id?` (import #${session.job_id})`:'';
 const messages={
  uploading:`${name}: upload incomplete. Select the same folder and Resume selected folder.`,
  queued:`${name}${job}: Stage 2 queued. Import starts when media work is idle.`,
  importing:`${name}${job}: Stage 2 importing / processing on the server.`,
  done:`${name}${job}: import complete.`,
  failed:`${name}${job}: needs attention. Review the error and Resume import when ready.`,
  paused:`${name}${job}: paused. Resume import when ready.`,
  cancelling:`${name}${job}: cancelling remaining work. Already imported media is retained.`,
  cancelled:`${name}${job}: cancelled. Already imported media and remaining temporary files are retained.`
 };
 const safe=session.handoff_ready&&session.job_id?' Stage 1 verified and saved to the server. Safe to close the browser or disconnect.':'';
 const cleanup=session.state==='done'?(session.staging_cleaned?' Temporary copies removed.':' Temporary copies retained until destination and processing checks pass.'):'';
 const bytes=uploadBytes(session);
 const pending=bytes.percent===100&&!session.handoff_ready?' Verification / server handoff not confirmed.':'';
 return bytes.text+'. '+(messages[session.state]||`${name}${job}: ${session.state}.`)+pending+safe+cleanup+(session.error?` ${session.error}`:'');
}

export function createUploadFeedback(render,storage){
 const key='familyfoto.selected-upload';
 let id=null,epoch=0,ticket=0,selected=false,held=false;
 try{id=storage?.getItem(key)||null;}catch{}
 const save=()=>{try{if(id)storage?.setItem(key,id);else storage?.removeItem(key);}catch{}};
 const current=token=>token===epoch;
 return {
  get id(){return id;},capture:()=>epoch,current,
  select(next=null){epoch++;ticket++;id=next;selected=true;held=false;save();render(next?'Loading upload status…':'Folder selected. Start upload, or resume a matching upload below.');return epoch;},
  attach(next,token){if(!current(token))return false;id=next;save();return true;},
  show(text,token=epoch){if(current(token))render(text);},
  error(text,token=epoch){if(current(token)){held=true;render(text);}},
  state(session,token=epoch){if(!current(token)||session.id!==id)return;ticket++;held=false;render(uploadMessage(session));},
  request(){return {epoch,ticket:++ticket};},
  fresh(token){return token.epoch===epoch&&token.ticket===ticket;},
  accept(sessions,token,busy=false){
   if(!this.fresh(token))return false;
   if(!id&&!selected&&sessions.length){id=sessions[0].id;save();}
   const session=sessions.find(s=>s.id===id);
   if(session&&!busy&&!(held&&session.state==='uploading'))render(uploadMessage(session));
   return true;
  }
 };
}
