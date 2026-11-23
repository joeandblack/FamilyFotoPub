// One user-initiated attachment for any multi-selection. Never buffer file bytes in JS.
export function setupDownloads({api}){
 const dialog=document.createElement('dialog');dialog.id='downloadDialog';dialog.setAttribute('aria-label','Download selected items');document.body.append(dialog);
 const style=document.createElement('link');style.rel='stylesheet';style.href='/web/downloads.css';document.head.append(style);
 let generation=0,controller=null,frame=null,sequence=0;
 function close(){generation++;controller?.abort();dialog.close();}
 dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
 window.addEventListener('message',e=>{
  if(e.origin!==location.origin||e.source!==frame?.contentWindow||e.data?.type!=='familyfoto-download-error')return;
  const status=dialog.querySelector('[role=status]');if(status)status.textContent=e.data.message;
 });
 return rows=>{
  const ids=[...new Set(rows.map(m=>m.id))];if(!ids.length)return;
  if(ids.length===1){const a=document.createElement('a');a.href=`/api/media/${ids[0]}/download`;a.download='';document.body.append(a);a.click();a.remove();return;}
  controller?.abort();controller=new AbortController();const ticket=++generation;
  dialog.replaceChildren();
  const heading=document.createElement('h2');heading.textContent='Download selected items';
  const detail=document.createElement('p');detail.textContent=`${ids.length} selected photos and videos will download together in one ZIP. Original file bytes are preserved; filenames include each media ID to avoid collisions.`;
  const status=document.createElement('p');status.setAttribute('role','status');status.textContent='Checking selected files…';
  const save=document.createElement('a');save.id='saveDownloadZip';save.className='download-save';save.textContent='Download ZIP';save.hidden=true;
  const cancel=document.createElement('button');cancel.textContent='Cancel';cancel.onclick=close;
  dialog.append(heading,detail,status,save,cancel);dialog.showModal();cancel.focus();
  api('/api/downloads/prepare?'+new URLSearchParams({ids:ids.join(',')}),{signal:controller.signal}).then(data=>{
   if(ticket!==generation)return;
   save.href=data.url;save.hidden=false;status.textContent=`Ready: ${data.count} items. This download uses the selection you made when you opened this dialog.`;save.focus();
   save.onclick=()=>{
    frame=document.createElement('iframe');frame.hidden=true;frame.name='familyfoto-download-'+Date.now().toString(36)+'-'+(++sequence);frame.title='Selected files download';document.body.append(frame);save.target=frame.name;
    frame.onload=()=>{try{const text=frame.contentDocument?.body?.textContent||'';if(text.startsWith('{')){const error=JSON.parse(text);if(error.error||error.detail)status.textContent=error.error||error.detail;}}catch{}};
    status.textContent='ZIP download requested. Check your browser’s Downloads for progress, completion, or cancellation. You can close this dialog.';cancel.textContent='Close';
   };
  }).catch(error=>{if(ticket===generation&&error.name!=='AbortError')status.textContent=error.message;});
 };
}
