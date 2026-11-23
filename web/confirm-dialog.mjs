let active=false;
const notices=[];

export function showNotice(message,{title='Unable to complete action'}={}){
 return new Promise(resolve=>{
  notices.push({message,title,resolve});
  drainNotices();
 });
}

function drainNotices(){
 if(active||!notices.length)return;
 const next=notices.shift();
 confirmAction(next.message,{title:next.title,confirmLabel:'Close',noticeOnly:true}).then(next.resolve);
}

// `content`, a node, goes under the message: for a decision that needs more than a sentence.
export function confirmAction(message,{title='Confirm action',confirmLabel='Confirm',danger=false,noticeOnly=false,content=null,retainOnConfirm=false}={}){
 if(active)return Promise.resolve(false);
 active=true;
 return new Promise(resolve=>{
  const previous=document.activeElement,dialog=document.createElement('dialog');
  dialog.className='ff-dialog confirm-dialog';
  dialog.setAttribute('aria-labelledby','ff-confirm-title');
  dialog.setAttribute('aria-describedby','ff-confirm-message');
  const heading=document.createElement('h2');heading.id='ff-confirm-title';heading.textContent=title;
  const description=document.createElement('p');description.id='ff-confirm-message';description.textContent=message;
  const actions=document.createElement('div');actions.className='confirm-dialog-actions';
  const cancel=document.createElement('button');cancel.type='button';cancel.textContent='Cancel';cancel.autofocus=true;cancel.className='btn btn-sm';
  const accept=document.createElement('button');accept.type='button';accept.textContent=confirmLabel;accept.className='btn btn-sm '+(danger?'confirm-danger':'btn-primary');
  let settled=false,retained=false;
  const finish=value=>{
   if(settled)return;settled=true;
   dialog.close();dialog.remove();active=false;
   if(previous?.isConnected)previous.focus({preventScroll:true});
   resolve(value);
   queueMicrotask(drainNotices);
  };
  cancel.onclick=()=>{if(!retained)finish(false);};
  accept.onclick=()=>{
   if(retained)return;
   if(!retainOnConfirm){finish(true);return;}
   retained=true;dialog.setAttribute('closedby','none');accept.disabled=true;cancel.disabled=true;
   resolve({dialog,close:()=>finish(true)});
  };
  dialog.addEventListener('cancel',event=>{event.preventDefault();if(!retained)finish(false);});
  dialog.addEventListener('keydown',event=>{event.stopPropagation();if(retained&&event.key==='Escape')event.preventDefault();});
  dialog.addEventListener('close',()=>finish(false));
  if(!noticeOnly)actions.append(cancel);actions.append(accept);dialog.append(heading,description,...(content?[content]:[]),actions);
  (document.fullscreenElement||document.body).append(dialog);
  dialog.showModal();(noticeOnly?accept:cancel).focus();
 });
}
