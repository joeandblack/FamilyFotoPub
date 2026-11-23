// Existing identities remain editable; new native inference needs migration.
function update(){
 for(const node of document.querySelectorAll('button')){
  if(['Detect again'].includes(node.textContent.trim())&&!node.disabled){
   node.disabled=true;
   node.title='Native face scanning is not connected to this existing People catalog yet.';
  }
 }
}
const begin=()=>{update();new MutationObserver(update).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled']});};
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',begin,{once:true});else begin();
