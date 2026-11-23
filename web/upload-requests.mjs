export class UploadTimeout extends Error{
 constructor(){super('Request timed out. Upload is safely paused if reconciliation cannot confirm progress; use Resume selected folder.');this.name='UploadTimeout';}
}
export function bounded(operation,{signal,timeoutMs=30000}={}){
 const controller=new AbortController();let timer,abort;
 const stop=new Promise((_,reject)=>{
  abort=()=>{controller.abort();reject(new DOMException('Upload paused','AbortError'));};
  if(signal?.aborted){abort();return;}
  signal?.addEventListener('abort',abort,{once:true});
  timer=setTimeout(()=>{controller.abort();reject(new UploadTimeout());},timeoutMs);
 });
 // Race includes body decoding, even if a browser/network ignores abort.
 return Promise.race([stop,Promise.resolve().then(()=>{if(controller.signal.aborted)throw new DOMException('Upload paused','AbortError');return operation(controller.signal);})])
  .finally(()=>{clearTimeout(timer);signal?.removeEventListener('abort',abort);});
}
export function uploadRequest(path,options={},fetcher=fetch){
 const {signal,timeoutMs=30000,...init}=options;
 return bounded(async inner=>{
  const response=await fetcher('/api/uploads'+path,{...init,signal:inner,headers:{'X-FamilyFoto-Upload':'1',...(init.body instanceof Blob?{}:{'Content-Type':'application/json'}),...init.headers}});
  const data=await response.json();
  if(!response.ok)throw Error(data.detail||data.error||'Upload request failed');
  return data;
 },{signal,timeoutMs});
}
export function backoff(attempt,signal){
 return bounded(()=>new Promise(resolve=>setTimeout(resolve,500*2**attempt)),{signal,timeoutMs:500*2**attempt+1000});
}
export async function sendChunk({request=uploadRequest,sid,fileId,offset,end,blob,sha256,signal,wait=backoff}){
 for(let attempt=0;attempt<3;attempt++){
  if(signal?.aborted)throw new DOMException('Upload paused','AbortError');
  try{
   const result=await request(`/${sid}/files/${fileId}?offset=${offset}`,{method:'PUT',body:blob,headers:{'X-Content-SHA256':sha256},signal,timeoutMs:120000});
   if(result.received!==end)throw Error('Unexpected acknowledged offset');
   return;
  }catch(error){
   if(signal?.aborted)throw new DOMException('Upload paused','AbortError');
   // Never blindly resend an ambiguously accepted write. A failed status read
   // pauses the transfer, rather than assuming nothing was committed.
   const status=await request('/'+sid,{signal,timeoutMs:15000});
   const file=status.files.find(f=>f.file_id===fileId);
   if(status.state!=='uploading'||status.cancel_requested)throw Error('Upload is no longer writable; refresh its status.');
   if(file?.received===end)return;
   if(file?.received!==offset)throw Error('Server offset changed. Safely paused; reselect the same folder and Resume.');
   if(attempt===2)throw Error('Upload paused after bounded retries: '+error.message);
   await wait(attempt,signal);
  }
 }
}
export async function handoff({request=uploadRequest,sid,signal,wait=backoff}){
 for(let attempt=0;attempt<3;attempt++){
  if(signal?.aborted)throw new DOMException('Upload paused','AbortError');
  try{
   const result=await request('/'+sid+'/finalize',{method:'POST',signal});
   if(result.handoff_ready&&result.job_id)return result;
   throw Error('Server handoff is not confirmed');
  }catch(error){
   if(signal?.aborted)throw new DOMException('Upload paused','AbortError');
   const current=await request('/'+sid,{signal,timeoutMs:15000});
   if(current.handoff_ready&&current.job_id)return current;
   if(current.state!=='uploading'||attempt===2)throw Error('Handoff not confirmed. Keep this folder available and Resume: '+error.message);
   await wait(attempt,signal);
  }
 }
}
