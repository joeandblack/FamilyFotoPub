import {backupActive} from '../../web/backup-state.mjs';
export function setupFaceService({api}){
 const section=document.createElement('details');
 section.className='settings-section';
 section.innerHTML=`<summary>Face detection</summary>
 <label><input type="checkbox" class="face-service-auto"> Start face detection with FamilyFoto</label>
 <div class="toolbar"><button type="button" data-service="start">Start</button><button type="button" data-service="stop">Stop</button></div>
 <p class="muted run-state" role="status"><span class="dot idle" aria-hidden="true"></span><span class="face-service-message">Checking face detection service...</span></p>`;
 document.getElementById('storage').prepend(section);
 const message=section.querySelector('.face-service-message'),auto=section.querySelector('input'),dot=section.querySelector('.dot');
 const buttons=[...section.querySelectorAll('button')];let loading=false,saving=false,preferenceRevision=0;
 function render(value,revision=preferenceRevision){
  message.textContent=value.message;
  dot.className='dot '+(value.state==='ready'?'ready':['starting','stopping'].includes(value.state)?'running':value.state==='unavailable'?'error':'idle');
  if(!saving&&revision===preferenceRevision&&typeof value.start_with_app==='boolean')auto.checked=value.start_with_app;
  buttons.forEach(button=>{button.disabled=value.busy||(button.dataset.service==='start'?value.state==='ready':value.state!=='ready');});
 }
 async function load(){
  section.hidden=document.body.dataset.role!=='admin';if(section.hidden||loading||backupActive())return;
  loading=true;const revision=preferenceRevision;
  try{const value=await api('/api/face-service');if(!backupActive())render(value,revision);}catch(error){if(!backupActive()){message.textContent=error.message;dot.className='dot error';}}finally{loading=false;}
 }
 buttons.forEach(button=>{button.onclick=async()=>{
  buttons.forEach(b=>{b.disabled=true;});dot.className='dot running';
  try{render(await api('/api/face-service/'+button.dataset.service,{method:'POST'}));}
  catch(error){message.textContent=error.message;dot.className='dot error';}
 };});
 auto.onchange=async()=>{
  const selected=auto.checked;preferenceRevision++;saving=true;auto.disabled=true;
  try{const result=await api('/api/face-service/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_with_app:selected})});auto.checked=result.start_with_app;}
  catch(error){auto.checked=!selected;message.textContent=error.message;}
  finally{preferenceRevision++;saving=false;auto.disabled=false;}
 };
 setInterval(()=>{if(!document.getElementById('storage').hidden&&!document.hidden)load();},3000);
 return {load};
}
