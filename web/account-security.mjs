import {confirmAction} from './confirm-dialog.mjs';

export function setupAccountSecurity({api,user,notice,navigate=url=>location.replace(url)}){
 const section=document.createElement('details');
 section.id='accountSecurity';
 section.className='settings-section';
 section.innerHTML=`<summary>Account security</summary>
  <p class="muted" data-account></p>
  <div class="toolbar"><button type="button" data-signout-all>Sign out all my sessions</button></div>
  <div class="toolbar" data-admin hidden><select aria-label="Account to sign out"></select>
   <button type="button" data-revoke>Sign out account</button></div>
  <p role="status" class="muted" data-status></p>`;
 section.querySelector('[data-account]').textContent=user.username;
 document.getElementById('storage').prepend(section);
 const signout=document.createElement('button');
 signout.type='button';signout.className='btn btn-sm';signout.title='Sign out this browser';
 signout.innerHTML='<i data-lucide="log-out" aria-hidden="true"></i> Sign out';
 document.querySelector('aside nav').after(signout);
 globalThis.lucide?.createIcons();
 const status=section.querySelector('[data-status]');
 let pending=false;
 async function command(path,username){
  if(pending)return;
  pending=true;
  const controls=[signout,...section.querySelectorAll('button,select')];
  controls.forEach(control=>control.disabled=true);
  status.textContent='Signing out...';
  try{
   const result=await api(path,{method:'POST',...(username?{body:JSON.stringify({username})}:{})});
   if(!username||username===user.username){navigate('/login');return;}
   status.textContent=`Signed out ${result.revoked} session${result.revoked===1?'':'s'}.`;
  }catch(error){status.textContent=error.message;notice(error.message);}
  finally{pending=false;controls.forEach(control=>control.disabled=false);}
 }
 signout.onclick=()=>command('/api/auth/logout');
 section.querySelector('[data-signout-all]').onclick=async()=>{
  if(await confirmAction('Sign out this account on every browser, including this one? Running media jobs will continue.',
     {title:'Sign out all sessions?',confirmLabel:'Sign out'}))command('/api/auth/logout-all');
 };
 if(user.role==='admin'){
  const admin=section.querySelector('[data-admin]'),select=admin.querySelector('select');
  api('/api/auth/accounts').then(({accounts})=>{
   for(const name of accounts)select.add(new Option(name,name));
   admin.hidden=false;
  }).catch(error=>{status.textContent=error.message;});
  section.querySelector('[data-revoke]').onclick=async()=>{
   const username=select.value;
   if(await confirmAction(`Sign out all browser sessions for ${username}? Running media jobs will continue.`,
      {title:'Sign out account?',confirmLabel:'Sign out'}))command('/api/auth/revoke',username);
  };
 }
 return section;
}
