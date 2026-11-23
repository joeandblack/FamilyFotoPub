import {confirmAction} from './confirm-dialog.mjs';

export const ROLE_LABELS={admin:'Admin',regular:'Normal',guest:'Guest'};
const ALPHABET='ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';

// Unambiguous letters and digits, chosen without modulo bias.
export function generatePassword(length=16,random=globalThis.crypto){
 let result='';
 const limit=256-256%ALPHABET.length;
 while(result.length<length){
  for(const byte of random.getRandomValues(new Uint8Array(length*2))){
   if(byte<limit&&result.length<length)result+=ALPHABET[byte%ALPHABET.length];
  }
 }
 return result;
}

// A self-reset signs you out at once, so the new password must be saved first.
export function selfResetProblem(selected,current,saved){
 return selected===current&&!saved?'Copy or save the new password, then tick the box to confirm, before resetting your own password.':null;
}

export function setupUserManagement({api,user,notice,navigate=url=>location.replace(url)}){
 if(user.role!=='admin')return null;
 const section=document.createElement('details');
 section.id='userManagement';
 section.className='settings-section';
 section.innerHTML=`<summary>User Management</summary>
  <table class="user-list"><thead><tr><th scope="col">Username</th><th scope="col">Role</th></tr></thead><tbody data-users></tbody></table>
  <h4>Add a user</h4>
  <form data-create class="user-form">
   <label>Username <input name="username" autocomplete="off" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9._\\-]{0,63}" spellcheck="false"></label>
   <label>Password <span class="user-password"><input name="password" autocomplete="new-password" required minlength="8" maxlength="1024" spellcheck="false"><button type="button" data-generate>Generate</button></span></label>
   <label>Role <select name="role"><option value="admin">Admin</option><option value="regular" selected>Normal</option><option value="guest">Guest</option></select></label>
   <label>Your administrator password <input type="password" name="admin_password" autocomplete="current-password" required maxlength="1024"></label>
   <button type="submit">Create user</button>
  </form>
  <h4>Reset a password</h4>
  <form data-reset class="user-form">
   <label>User <select name="username" required></select></label>
   <label>New password <span class="user-password"><input name="password" autocomplete="new-password" required minlength="8" maxlength="1024" spellcheck="false"><button type="button" data-generate>Generate</button></span></label>
   <label class="user-saved" data-saved hidden><input type="checkbox" name="saved"> I have copied or saved this new password</label>
   <label>Your administrator password <input type="password" name="admin_password" autocomplete="current-password" required maxlength="1024"></label>
   <button type="submit">Reset password</button>
  </form>
  <p class="muted">Copy the password and give it to the person; FamilyFoto stores only a hash and cannot show it again. Resetting a password signs that user out everywhere.</p>
  <p role="status" class="muted" data-status></p>`;
 const anchor=document.getElementById('accountSecurity');
 if(anchor)anchor.after(section);else document.getElementById('storage').prepend(section);
 const status=section.querySelector('[data-status]');
 const create=section.querySelector('[data-create]'),reset=section.querySelector('[data-reset]');
 for(const form of [create,reset]){
  form.elements.password.value=generatePassword();
  form.querySelector('[data-generate]').onclick=()=>{form.elements.password.value=generatePassword();};
 }
 function show(users){
  section.querySelector('[data-users]').replaceChildren(...users.map(account=>{
   const row=document.createElement('tr'),name=document.createElement('td'),role=document.createElement('td');
   name.textContent=account.username;
   role.textContent=(ROLE_LABELS[account.role]||account.role)+(account.primary?' (main administrator)':'');
   row.append(name,role);return row;
  }));
  const select=reset.elements.username,previous=select.value;
  select.replaceChildren(...users.map(account=>new Option(account.username,account.username)));
  if(users.some(account=>account.username===previous))select.value=previous;
  showSaved();
 }
 // Shown only for your own account; any new password clears the tick.
 function showSaved(){
  reset.querySelector('[data-saved]').hidden=reset.elements.username.value!==user.username;
  reset.elements.saved.checked=false;
 }
 reset.elements.password.addEventListener('input',()=>{reset.elements.saved.checked=false;});
 reset.querySelector('[data-generate]').addEventListener('click',()=>{reset.elements.saved.checked=false;});
 let pending=false;
 async function send(path,body,message,form){
  if(pending)return null;
  body.admin_password=form.elements.admin_password.value;
  form.elements.admin_password.value="";
  pending=true;
  const controls=section.querySelectorAll('button,input,select');
  controls.forEach(control=>control.disabled=true);
  status.textContent='Saving...';
  try{
   const result=await api(path,{method:'POST',body:JSON.stringify(body)});
   status.textContent=message;
   return result;
  }catch(error){status.textContent=error.message;notice(error.message);return null;}
  finally{pending=false;controls.forEach(control=>control.disabled=false);}
 }
 create.onsubmit=async event=>{
  event.preventDefault();
  const username=create.elements.username.value.trim(),password=create.elements.password.value,role=create.elements.role.value;
  const result=await send('/api/auth/users',{username,password,role},`Created ${username} (${ROLE_LABELS[role]}). Give them the password now.`,create);
  // Keep the password on screen to copy; a fresh one replaces it when the next user is typed.
  if(result){show(result.users);create.elements.username.value='';create.dataset.used='true';}
 };
 create.elements.username.addEventListener('input',()=>{
  if(create.dataset.used){delete create.dataset.used;create.elements.password.value=generatePassword();}
 });
 reset.elements.username.addEventListener('change',()=>{
  if(reset.dataset.used){delete reset.dataset.used;reset.elements.password.value=generatePassword();}
  showSaved();
 });
 reset.onsubmit=async event=>{
  event.preventDefault();
  const username=reset.elements.username.value,password=reset.elements.password.value,self=username===user.username;
  const problem=selfResetProblem(username,user.username,reset.elements.saved.checked);
  if(problem){status.textContent=problem;reset.elements.saved.focus();return;}
  if(!await confirmAction(self?'Reset your own password? You will be signed out now and must sign in with the new password you saved.':
     `Reset the password for ${username}? They will be signed out on every browser.`,{title:'Reset password?',confirmLabel:'Reset password'}))return;
  const result=await send('/api/auth/users/password',{username,password},`Password reset for ${username}. Give them the new password now.`,reset);
  if(result?.signed_out){navigate('/login');return;}
  if(result)reset.dataset.used='true';
 };
 api('/api/auth/users').then(({users})=>show(users)).catch(error=>{status.textContent=error.message;});
 return section;
}
