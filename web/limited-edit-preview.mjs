// Metadata editing only. The server allowlist and database grants enforce this.
const blocked=/^(move to trash|permanently delete|start|resume|pause|stop|scan|detect again|auto-organize|undo auto-organize|convert|upload|run |crop|rotate|select source|send to review|confirm match|not this person)/i;
function update(){
 const subtitle=document.querySelector('aside>p');
 if(subtitle&&subtitle.textContent!=='Limited editing · Media protected')subtitle.textContent='Limited editing · Media protected';
 const status=document.querySelector('#sideState');
 if(status&&status.textContent!=='Editing ready · Jobs disabled')status.textContent='Editing ready · Jobs disabled';
 const dot=document.querySelector('#sideDot');
 if(dot&&dot.className!=='dot complete')dot.className='dot complete';
 for(const node of document.querySelectorAll('button,input[type=submit]')){
  const labels=[node.textContent.trim(),node.getAttribute('aria-label')||'',node.title||''];
  if(labels.some(label=>blocked.test(label.trim()))&&!node.disabled){node.disabled=true;node.title='Unavailable during limited editing';}
 }
}
const begin=()=>{update();new MutationObserver(update).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled']});};
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',begin,{once:true});else begin();
