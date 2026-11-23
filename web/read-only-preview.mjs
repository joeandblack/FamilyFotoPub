const mutating=/^(create|new |edit|rename|match |delete|remove|move|add |use as |adjust |set as |save|start|resume|pause|stop|scan|detect|auto-organize|undo|restore|ignore |convert|upload|change |run |crop|rotate|select source|confirm match|not this person|review blurry|release)/i;
function update(){
 const subtitle=document.querySelector('aside>p');
 if(subtitle&&subtitle.textContent!=='Read-only preview · Changes disabled')subtitle.textContent='Read-only preview · Changes disabled';
 const status=document.querySelector('#sideState');
 if(status&&status.textContent!=='Read-only preview')status.textContent='Read-only preview';
 const dot=document.querySelector('#sideDot');
 if(dot&&dot.className!=='dot complete')dot.className='dot complete';
 for(const node of document.querySelectorAll('button,input[type=submit]')){
  const labels=[node.textContent.trim(),node.getAttribute('aria-label')||'',node.title||''];
  if((labels.some(label=>mutating.test(label.trim()))||node.classList.contains('editable-person-name')||node.classList.contains('restore-ignored')||node.textContent.trim()==='Ignore')&&!node.disabled){node.disabled=true;node.title='Unavailable in read-only preview';}
 }
 for(const node of document.querySelectorAll('[draggable=true]'))node.draggable=false;
 for(const node of document.querySelectorAll('[contenteditable=true]'))node.contentEditable='false';
}
const begin=()=>{update();new MutationObserver(update).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','draggable','contenteditable']});};
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',begin,{once:true});else begin();
