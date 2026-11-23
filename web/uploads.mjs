import {size} from './size.mjs';
import {confirmAction} from './confirm-dialog.mjs';
import {uploadRequest,bounded,sendChunk,handoff} from './upload-requests.mjs';
import {createUploadFeedback,uploadBytes} from './upload-feedback.mjs';

export function selectionManifest(files){return [...files].map(f=>({path:f.webkitRelativePath,size:f.size,modified:f.lastModified})).sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0);}
export function sameSelection(manifest,session){return manifest.length===session.files.length&&manifest.every((f,i)=>{const old=session.files[i];return f.path===old.relative_path&&f.size===old.size&&f.modified===old.modified_ms;});}
export function requestKey(){const a=crypto.getRandomValues(new Uint8Array(16));a[6]=(a[6]&15)|64;a[8]=(a[8]&63)|128;const s=[...a].map(v=>v.toString(16).padStart(2,'0')).join('');return `${s.slice(0,8)}-${s.slice(8,12)}-${s.slice(12,16)}-${s.slice(16,20)}-${s.slice(20)}`;}

// Current access, not the choice made at upload time: later changes must not leave a stale "Private" label.
export function visibilityNote(s){
 if(s.visibility==='private')return ' · Private';
 if(s.visibility==='mixed')return ` · Mixed (${s.visibility_counts.private} private, ${s.visibility_counts.shared} shared)`;
 return !s.visibility&&s.state!=='done'&&s.requested_visibility==='private'?' · Private once imported':'';
}
export function setupUploads(container,{notice,refresh,beforeStart=async()=>{},options,api}){
 container.innerHTML='<label for="uploadFolder">Folder on this computer</label><input id="uploadFolder" type="file" webkitdirectory multiple hidden><div class="manual-source"><button id="browseUpload" type="button">Browse folder</button><span id="uploadSelection">No folder selected</span><button id="startUpload" type="button">Start upload</button><button id="pauseUpload" type="button" hidden>Pause upload</button></div><p class="muted">1. Upload and verify all files in server temporary storage. Keep this tab open until handoff is confirmed. 2. The server imports into the library and removes verified temporary copies. Your originals stay on this computer.</p><div id="uploadProgress" role="status" aria-live="polite"></div><progress id="uploadMeter" max="100" value="0" aria-label="Upload bytes acknowledged"></progress><div id="uploadSessions"></div>';
 const $=id=>container.querySelector('#'+id);let files=[],key=null,running=false,controller=null,worker=null,caps=null;
 const feedback=createUploadFeedback(text=>{$('uploadProgress').textContent=text;},sessionStorage);
 const request=uploadRequest;
 function message(text,token){feedback.show(text,token);}
 function setRunning(value){running=value;$('startUpload').disabled=$('browseUpload').disabled=value;$('pauseUpload').hidden=!value;}
 function hash(action,bytes){return bounded(()=>new Promise((resolve,reject)=>{worker.onmessage=e=>e.data.error?reject(Error(e.data.error)):resolve(e.data);worker.onerror=()=>reject(Error('Hash worker failed'));worker.postMessage({action,bytes},bytes?[bytes]:[]);}),{signal:controller.signal,timeoutMs:60000});}
 const stopped=()=>{if(controller.signal.aborted)throw new DOMException('Upload paused','AbortError');};
 async function transfer(session,token){
  if(!sameSelection(selectionManifest(files),session))throw Error('Reselect the same folder with the same files, sizes and modification times. Changed folders need a new upload.');
  if(!feedback.attach(session.id,token))return;const byPath=new Map(files.map(f=>[f.webkitRelativePath,f]));
  worker=new Worker('/web/upload-hash-worker.mjs',{type:'module'});
  let acknowledged=uploadBytes(session).received;
  const progress=()=>{const p=uploadBytes({...session,received:acknowledged});if(feedback.current(token))$('uploadMeter').value=p.percent;return p.text;};
  message('Stage 1 · '+progress()+' · Uploading / verifying; keep this tab open.',token);
  for(const item of session.files){
   if(item.disposition!=='accepted')continue;
   const file=byPath.get(item.relative_path);await hash('start');
   for(let offset=0;offset<file.size;){
    stopped();const end=Math.min(file.size,offset+(session.limits||caps).chunk_bytes,item.received>offset?item.received:Infinity);
    message(`Stage 1 · ${item.verified?'Checking selected file':'Uploading / verifying'} · ${item.relative_path} · ${progress()}`,token);
    const blob=file.slice(offset,end);const digest=await hash('chunk',await bounded(()=>blob.arrayBuffer(),{signal:controller.signal,timeoutMs:60000}));stopped();
    if(offset>=item.received&&!item.verified){
     await sendChunk({request,sid:session.id,fileId:item.file_id,offset,end,blob,sha256:digest.sha256,signal:controller.signal});
     acknowledged+=end-offset;message('Stage 1 · '+progress()+' · Verifying; keep this tab open.',token);
    }
    offset=end;
   }
   const digest=await hash('finish');stopped();
   if(item.verified){if(digest.sha256!==item.sha256)throw Error('Reselected file content changed: '+item.relative_path);}
   else{
    try{await request(`/${session.id}/files/${item.file_id}/verify`,{method:'POST',body:JSON.stringify({sha256:digest.sha256}),signal:controller.signal});}
    catch(error){if(!controller.signal.aborted&&error.message.includes('checksum differs')){await request(`/${session.id}/files/${item.file_id}/reset`,{method:'POST'});throw Error('Resumed content did not match. File reset safely; select Resume to upload it again.');}throw error;}
   }
  }
  stopped();message('Stage 1 - '+progress()+' - Files verified. Confirming server handoff; keep this tab open.',token);const done=await handoff({request,sid:session.id,signal:controller.signal});
  feedback.state(done,token);await refresh();
 }
 async function begin(session){
  if(running)return;const token=feedback.capture();setRunning(true);controller=new AbortController();
  try{
   if(!files.length)throw Error('Browse and select a folder first.');
   if(!session)await beforeStart();
   stopped();
   // Loading required transfer limits must not depend on a UI poll winning its
   // freshness race: a newer poll may legitimately supersede an older one.
   if(!caps)caps=(await request('',{signal:controller.signal})).limits;
   if(!feedback.current(token))return;
   if(!session)session=await request('',{method:'POST',body:JSON.stringify({request_key:key,files:selectionManifest(files),...options()}),signal:controller.signal});
   if(!feedback.current(token))return;await transfer(session,token);
  }catch(error){feedback.error(error.name==='AbortError'?'Upload paused. Reselect the folder after reopening this page, then Resume.':error.message,token);}
  finally{worker?.terminate();worker=null;setRunning(false);await update();}
 }
 $('browseUpload').onclick=()=>{$('uploadFolder').value='';$('uploadFolder').click();};
 $('uploadFolder').onchange=()=>{files=[...$('uploadFolder').files];key=requestKey();feedback.select();$('uploadMeter').value=0;$('uploadSelection').textContent=files.length?`${files[0].webkitRelativePath.split('/')[0]} · ${files.length} files · ${size(files.reduce((n,f)=>n+f.size,0))}`:'No folder selected';};
 $('startUpload').onclick=()=>begin();
 $('pauseUpload').onclick=()=>{controller?.abort();message('Pausing upload…');};
 async function update(){
  const token=feedback.request();
  try{const result=await request('');if(!feedback.fresh(token))return;caps=result.limits;
   const sessions=[...result.sessions];if(feedback.id&&!sessions.some(s=>s.id===feedback.id)){sessions.push(await request('/'+feedback.id));}
   if(!feedback.accept(sessions,token,running))return;const selected=sessions.find(s=>s.id===feedback.id);if(selected&&!running)$('uploadMeter').value=uploadBytes(selected).percent;const list=$('uploadSessions');list.replaceChildren();
   for(const session of result.sessions){const row=document.createElement('section');row.className='upload-session';const title=document.createElement('p');title.textContent=`Upload · ${session.label}${document.body.dataset.role==='admin'&&session.owner?' · by '+session.owner:''}${visibilityNote(session)} · ${session.state}${session.job_id?' · Import #'+session.job_id:''} · ${uploadBytes(session).text} · Accepted ${session.counts.accepted}, ignored ${session.counts.ignored}, unsupported ${session.counts.unsupported}${session.error?' · '+session.error:''}`;row.append(title);
    const button=(label,fn)=>{const b=document.createElement('button');b.textContent=label;b.type='button';b.onclick=async()=>{const context={token:feedback.capture()};b.disabled=true;try{await fn(context);}catch(e){feedback.error(e.message,context.token);}finally{await update();}};row.append(b);};
    if(session.state==='uploading')button('Resume selected folder',async context=>{if(running)return;const selection=context.token=feedback.select(session.id);const current=await request('/'+session.id);if(feedback.current(selection))await begin(current);});
    if(['failed','paused'].includes(session.state))button('Resume import',async context=>{const selection=context.token=feedback.select(session.id);const current=await request('/'+session.id+'/retry',{method:'POST'});feedback.state(current,selection);});
    if(!['done','cancelled'].includes(session.state))button('Cancel',async context=>{if(!await confirmAction('Cancel remaining upload/import work? Already imported media stays in the library.',{title:'Cancel upload?',confirmLabel:'Cancel upload',danger:true}))return;if(feedback.id===session.id)controller?.abort();const selection=context.token=feedback.select(session.id);const current=await request('/'+session.id+'/cancel',{method:'POST'});feedback.state(current,selection);});
    list.append(row);
   }
  }catch(e){if(feedback.fresh(token)&&!running)message(e.message,token.epoch);}
 }
 return {update};
}
