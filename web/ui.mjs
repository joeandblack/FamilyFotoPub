// Local daisyUI components decorate native controls without changing their behavior.
// Gallery cards and the full-screen media player retain their purpose-built layouts.
export function decorate(root=document){
 const nodes=[];if(root.nodeType===1)nodes.push(root);
 if(root.querySelectorAll)nodes.push(...root.querySelectorAll('button,input,select,textarea,progress,dialog,.notice'));
 for(const node of nodes){
  if(node.matches('button')&&!node.matches('.tile,.album-card')&&!node.closest('#viewer,.cropper-container')){
   node.classList.add('btn','btn-sm');
   if(node.matches('.album-primary,#startImport,#uploadStart,#loginForm [type="submit"]'))node.classList.add('btn-primary');
   if(node.closest('nav'))node.classList.add('btn-ghost');
  }else if(node.matches('input')){
   if(node.type==='checkbox')node.classList.add('checkbox','checkbox-sm','checkbox-primary');
   else if(node.type==='radio')node.classList.add('radio','radio-sm','radio-primary');
   else if(!['range','hidden','file'].includes(node.type))node.classList.add('input','input-sm');
  }else if(node.matches('select'))node.classList.add('select','select-sm');
  else if(node.matches('textarea'))node.classList.add('textarea');
  else if(node.matches('progress'))node.classList.add('progress','progress-primary');
  else if(node.matches('dialog:not(#viewer)'))node.classList.add('ff-dialog');
  if(node.matches('.notice'))node.classList.add('alert','alert-soft','alert-warning');
 }
}
decorate();
new MutationObserver(records=>{for(const record of records)for(const node of record.addedNodes)if(node.nodeType===1)decorate(node);}).observe(document.body,{childList:true,subtree:true});
