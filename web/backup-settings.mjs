import {confirmAction} from './confirm-dialog.mjs';
import {backupActive} from './backup-state.mjs';
import {size} from './size.mjs';
import {progressText,retainedAttempts,mountProgressDialog,startProgress} from './backup-progress.mjs';

const stateLabels={starting:'Starting',['stopping-app']:'Stopping the app',capturing:'Backing up',verifying:'Verifying',failed:'Failed, waiting for a decision',restarting:'Restarting the app',finished:'Finished'};
export function runOutcome(run){
 if(run.state!=='finished')return stateLabels[run.state]||run.state;
 if(run.verified_at)return 'Verified';
 return run.gave_up?'Failed, not a recovery point':'Not verified';
}
const restoreLabels={starting:'Starting',['checking-backup']:'Checking the backup',['preparing-database']:'Preparing a temporary database',restoring:'Restoring',validating:'Checking the restored copy',['stopping-database']:'Finishing',finished:'Restored and checked',failed:'Failed'};
export function restoreOutcome(run){return restoreLabels[run.state]||run.state;}
export function restoreStatusText(run){const detail=progressText(run.progress);return `${restoreOutcome(run)}…${detail?` ${detail}`:''}`;}
export function restoreRunning(run){return !['finished','failed'].includes(run.state);}
export function durationText(seconds){
 const minutes=Math.max(1,Math.round(seconds/60));
 if(minutes<90)return `${minutes} minute${minutes===1?'':'s'}`;
 const hours=Math.round(minutes/60);return `${hours} hours`;
}
export function backupTotalTime(run){
 if(run.state!=='finished'||!run.started_at||!run.finished_at)return '\u2014';
 const elapsed=Date.parse(run.finished_at)-Date.parse(run.started_at);
 if(!Number.isFinite(elapsed)||elapsed<0)return '\u2014';
 const seconds=Math.floor(elapsed/1000),hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60);
 return `${hours?`${hours}h `:''}${hours||minutes?`${minutes}m `:''}${seconds%60}s`;
}
// Honest about where the number comes from: the last verified backup, or none yet.
export function estimateText(estimate){
 if(estimate)return `The last backup, of ${size(estimate.from_media_bytes)}, took ${durationText(estimate.last_seconds)}, so expect about ${durationText(estimate.seconds)} this time.`;
 return 'There is no earlier backup to estimate from. As a guide, a USB disk copies roughly 20 to 100 MB a second, so each terabyte takes about 3 to 14 hours to copy, and about as long again to check.';
}
export function nextStepsText(runs){
 const latest=(runs||[]).find(run=>run.state==='finished');
 if(!latest?.verified_at)return '';
 return `The latest backup is verified, in ${latest.directory}. To move the library to another computer, take that disk to it. On the other computer, choose Restore from a backup when you install FamilyFoto, and enter the administrator password this backup was made with.`;
}
// Everything the administrator should know before the library goes offline, in the order it happens.
export function startBackupSections(plan,{destination,unprotected,copyStrategy='fast-full'}){
 const time=plan?.estimate?`about ${durationText(plan.estimate.seconds)}`:'many hours for a large library';
 return [
  {heading:'What happens',items:[
   'FamilyFoto stops for everyone. Anyone using it, on any device, loses the page until the backup is finished.',
   'This dialog stays open with progress and an expandable backup log. The rest of the app is unavailable while the backup runs.',
   `The whole library, ${size(plan?.media_bytes??0)} of photos and videos with a ${size(plan?.database_bytes??0)} catalog, is copied into a new folder inside ${destination}.`,
   'Every file in the copy is then read back and checked against the library.',
   'When the check passes, FamilyFoto starts again by itself, and the progress page links back to it.']},
  {heading:'Before you start',items:[
   `It takes ${time}. Keep this computer on, and don't let it sleep.`,
   'Keep the backup disk connected until the progress page says the backup is finished.',
   'Your administrator password protects the catalog in this backup. Restoring it needs this same password, even if you change it later, and FamilyFoto cannot reset it. Photos and videos on the backup disk can still be read without it.',
   ...(unprotected?['You allow a disk with no file permissions: anyone with that disk can read your photos, videos and file names. The catalog stays encrypted.']:[])]},
  {heading:'If something goes wrong',items:[
   'Nothing in your library is changed or deleted. The backup only adds a new folder on the backup disk.',
   copyStrategy!=='standard'?'If the backup stops part way, its folder is kept. Start new attempt copies everything into another folder and requires space for that whole copy, or give up and start FamilyFoto again.':'If the backup stops part way, FamilyFoto stays stopped and the progress page asks you to choose: try again, or give up and start FamilyFoto again.']}];
}
export function backupOptions(strategy){
 if(!['standard','fast-full'].includes(strategy))throw new Error('Choose Standard or Fast. Both verify every file.');
 return {copy_strategy:strategy,verification:'full'};
}
export function copyMethodDescription(strategy){
 return strategy==='fast-full'?'Faster copying, followed by full verification of every file. Interrupted backups start a new copy.':'Resumable copying with full verification of every file. Interrupted backups can be retried.';
}
function sectionsNode(sections){
 const node=document.createElement('div');node.className='confirm-dialog-sections';
 for(const {heading,items} of sections){
  const title=document.createElement('h3');title.textContent=heading;
  const list=document.createElement('ul');list.append(...items.map(text=>{const item=document.createElement('li');item.textContent=text;return item;}));
  node.append(title,list);
 }
 return node;
}
export function setupBackupSettings({api}){
 const section=document.createElement('details');section.id='backupSettings';
 section.className='settings-section';
 section.innerHTML=`<summary>Backup and recovery</summary>
 <p id="backupIntro"></p>
 <fieldset id="backupStrategyField" aria-describedby="backupStrategyDescription"><legend>Copy method</legend>
 <div class="backup-method-options">
 <label class="backup-method"><input id="backupStrategyFast" type="radio" name="backupStrategy" value="fast-full" checked aria-describedby="backupFastHelp"> Fast backup<span id="backupFastHelp" class="backup-method-tooltip" role="tooltip">${copyMethodDescription('fast-full')}</span></label>
 <label class="backup-method"><input id="backupStrategyStandard" type="radio" name="backupStrategy" value="standard" aria-describedby="backupStandardHelp"> Standard backup<span id="backupStandardHelp" class="backup-method-tooltip" role="tooltip">${copyMethodDescription('standard')}</span></label>
 </div><p id="backupStrategyDescription" class="muted" role="status"></p></fieldset>
 <label for="backupDestination">Backup folder</label>
 <p class="muted">Each backup goes into a new folder inside this one. Choose a disk other than the one holding your photos, such as an external disk.</p>
 <div class="manual-source"><input id="backupDestination" autocomplete="off"><button id="browseBackupDestination" type="button">Browse</button></div>
 <label for="backupPassword">Your administrator password</label>
 <p id="backupPasswordHelp" class="muted">It protects the catalog in the backup, so there is no separate key to keep. Restoring the backup needs this same password, even if you change it later.</p>
 <div class="manual-source"><input id="backupPassword" type="password" autocomplete="current-password" aria-describedby="backupPasswordHelp"><label class="inline-check"><input id="showBackupPassword" type="checkbox"> Show</label></div>
 <label id="backupUnprotectedLabel"><input id="backupUnprotected" type="checkbox"> Allow a disk without file permissions, such as exFAT. I accept that anyone with the disk can read my photos, videos and file names. The catalog stays encrypted.</label>
 <button id="startBackup" type="button" disabled>Start backup</button>
 <p id="backupStatus" role="status"></p>
 <p id="backupNextSteps" hidden></p>
 <h4>Past backups</h4><div class="backup-history-scroll"><table id="backupRuns" class="backup-runs"><thead><tr><th scope="col">Started</th><th scope="col">Result</th><th scope="col" title="Elapsed time from start through finish, including waits and restart">Total time</th><th scope="col">Files</th><th scope="col">Size</th><th scope="col">Folder</th></tr></thead><tbody></tbody></table></div><p id="backupRunsEmpty" class="muted">No backups yet.</p>
 <h4>Test a restore</h4>
 <p>This proves a backup can be restored, by restoring it into a new private folder with its own temporary database. FamilyFoto keeps running, and your library isn't touched. The copy isn't switched on, and you can delete its folder afterwards.</p>
 <label for="restoreBackup">Backup</label><select id="restoreBackup"></select>
 <label for="restorePassword">Administrator password when this backup was made</label>
 <input id="restorePassword" type="password" autocomplete="off">
 <label for="restoreParent">Folder for the restored copy</label>
 <div class="manual-source"><input id="restoreParent" autocomplete="off"><button id="browseRestoreParent" type="button">Browse</button></div>
 <button id="startRestore" type="button">Start restore test</button>
 <p id="restoreStatus" role="status"></p>
 <div style="overflow-x:auto;max-width:100%"><table id="restoreRuns" class="backup-runs" style="min-width:640px"><thead><tr><th scope="col">Started</th><th scope="col">Result</th><th scope="col">Files</th><th scope="col">Size</th><th scope="col">Folder</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody></tbody></table></div>`;
 document.getElementById('storage').prepend(section);
 const $=id=>document.getElementById(id);let data=null,pending=false,edited=false;
 const status=text=>{$('backupStatus').textContent=text;};
 const post=(url,body)=>api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const selectedMethod=()=>section.querySelector('input[name="backupStrategy"]:checked').value;
 function controls(){
  const unsupported=!data||Boolean(data.unsupported);
  for(const id of ['backupDestination','backupPassword','browseBackupDestination','backupStrategyField','backupUnprotected'])$(id).disabled=pending||unsupported;
  $('startBackup').disabled=pending||unsupported;
 }
 function resetPlan(){$('backupUnprotected').checked=false;controls();}
 function history(runs){
  const rows=$('backupRuns').tBodies[0];rows.replaceChildren();$('backupRunsEmpty').hidden=runs.length>0;$('backupRuns').hidden=runs.length===0;
  for(const run of runs){const row=rows.insertRow();
   for(const text of [new Date(run.started_at).toLocaleString(),runOutcome(run),backupTotalTime(run),run.media_files??'—',run.media_bytes===undefined?'—':size(run.media_bytes),run.directory])row.insertCell().textContent=text;
   const retained=retainedAttempts(run);
   if(retained.length){const title=document.createElement('p');title.textContent='Retained incomplete attempts';const list=document.createElement('ul');list.style.color='inherit';
    for(const path of retained){const item=document.createElement('li');item.textContent=path;list.append(item);}
    const folder=rows.insertRow().insertCell();folder.colSpan=row.cells.length;folder.style.overflowWrap='anywhere';folder.append(title,list);}}
 }
 async function load(){
  section.hidden=document.body.dataset.role!=='admin';if(section.hidden)return;
  try{
   data=await api('/api/backup');
   describeMethod();
   if(!edited)$('backupDestination').value=data.preferences.destination;
   $('backupIntro').textContent=data.unsupported||`A backup copies the whole library (about ${size(data.media_bytes)} of media and a ${size(data.database_bytes)} catalog). Both copy methods verify every copied file. FamilyFoto stops while copying and restarts when verification passes. ${estimateText(data.estimate)}`;
   const next=nextStepsText(data.runs);$('backupNextSteps').textContent=next;$('backupNextSteps').hidden=!next;
   history(data.runs);
   await loadRestores();
  }catch(error){status(error.message);}finally{controls();}
 }
 let restoreData=null,restoreTimer=null;
 function restoreControls(){
  const running=restoreData?.runs.some(restoreRunning);
  $('startRestore').disabled=pending||running||!$('restoreBackup').value||!$('restoreParent').value.trim()||!$('restorePassword').value;
  $('browseRestoreParent').disabled=pending;
 }
 function restoreHistory(runs){
  const rows=$('restoreRuns').tBodies[0];rows.replaceChildren();$('restoreRuns').hidden=runs.length===0;
  for(const run of runs){const row=rows.insertRow();
   for(const text of [new Date(run.started_at).toLocaleString(),restoreOutcome(run),run.media_files??'—',run.media_bytes===undefined?'—':size(run.media_bytes),run.directory])row.insertCell().textContent=text;
   const actions=row.insertCell();
   if(run.state==='failed'){const retry=document.createElement('button');retry.type='button';retry.textContent='Try again';retry.onclick=()=>retryRestore(run.id);actions.append(retry);}}
 }
 async function loadRestores(){
  clearTimeout(restoreTimer);
  if(backupActive())return;
  restoreData=await api('/api/backup/restores');
  if(backupActive())return;
  const select=$('restoreBackup'),chosen=select.value;
  select.replaceChildren(...restoreData.backups.map(backup=>{const option=document.createElement('option');option.value=backup.directory;option.textContent=`${new Date(backup.started_at).toLocaleString()}, ${backup.media_files??'?'} files`;return option;}));
  if(chosen&&[...select.options].some(option=>option.value===chosen))select.value=chosen;
  restoreHistory(restoreData.runs);
  const running=restoreData.runs.find(restoreRunning),latest=restoreData.runs[0];
  $('restoreStatus').textContent=running?restoreStatusText(running):latest?.state==='failed'?`The restore test failed: ${latest.error||'see the log'}`:restoreData.backups.length?'':'Finish and verify a backup first.';
  restoreControls();
  if(running&&document.getElementById('backupSettings')?.isConnected)restoreTimer=setTimeout(()=>loadRestores().catch(error=>{$('restoreStatus').textContent=error.message;}),2000);
 }
 // Each attempt is a new process, and no password is kept between them, so a retry asks again.
 async function retryRestore(id){
  if(!$('restorePassword').value){$('restoreStatus').textContent='Enter the administrator password this backup was made with, then try again.';$('restorePassword').focus();return;}
  try{await post(`/api/backup/restore/${id}/retry`,{password:$('restorePassword').value});$('restorePassword').value='';await loadRestores();}catch(error){$('restoreStatus').textContent=error.message;}
 }
 $('restoreParent').oninput=restoreControls;$('restoreBackup').onchange=restoreControls;$('restorePassword').oninput=restoreControls;
 $('browseRestoreParent').onclick=async()=>{
  pending=true;restoreControls();
  try{const result=await api('/api/storage/select-folder',{method:'POST'});if(!result.cancelled)$('restoreParent').value=result.path;}
  catch(error){$('restoreStatus').textContent=error.message;}finally{pending=false;restoreControls();}
 };
 $('startRestore').onclick=async()=>{
  pending=true;restoreControls();$('restoreStatus').textContent='Starting…';
  try{await post('/api/backup/restore',{backup_directory:$('restoreBackup').value,password:$('restorePassword').value,parent:$('restoreParent').value});$('restorePassword').value='';await loadRestores();}
  catch(error){$('restoreStatus').textContent=error.message;}finally{pending=false;restoreControls();}
 };
 $('backupDestination').oninput=()=>{edited=true;resetPlan();};
 $('backupPassword').oninput=resetPlan;
 function describeMethod(){$('backupStrategyDescription').textContent=copyMethodDescription(selectedMethod());}
 $('backupStrategyField').onchange=()=>{describeMethod();resetPlan();};
 $('showBackupPassword').onchange=()=>{$('backupPassword').type=$('showBackupPassword').checked?'text':'password';};
 $('backupUnprotected').onchange=controls;
 $('browseBackupDestination').onclick=async()=>{
  pending=true;controls();status('Choose a folder on the FamilyFoto computer…');
  try{const result=await api('/api/storage/select-folder',{method:'POST'});if(!result.cancelled){$('backupDestination').value=result.path;edited=true;resetPlan();}status('');}
  catch(error){status(error.message);}finally{pending=false;controls();}
 };
 $('startBackup').onclick=async()=>{
  if(pending)return;
  const sections=startBackupSections(data,{destination:$('backupDestination').value.trim(),unprotected:$('backupUnprotected').checked,copyStrategy:selectedMethod()});
  const confirmation=await confirmAction('Read this before you start. Nothing happens until you choose Start backup.',{title:'Start the backup?',confirmLabel:'Start backup',content:sectionsNode(sections),retainOnConfirm:true});
  if(!confirmation)return;
  pending=true;controls();status('Starting…');
  clearTimeout(restoreTimer);
  const dialog=confirmation.dialog;
  mountProgressDialog(dialog);
  dialog.querySelector('#runAbout').textContent='The plan is checked before FamilyFoto stops. Keep the backup disk connected.';
  dialog.querySelector('#runState').textContent='Checking backup plan...';
  dialog.querySelector('#runProgress').hidden=false;
  dialog.querySelector('#runProgress').removeAttribute('value');
  const started=performance.now();
  const tick=()=>{dialog.querySelector('#runDetail').textContent=`Checking backup plan and starting: ${Math.floor((performance.now()-started)/1000)}s elapsed`;};
  tick();const timer=setInterval(tick,1000);
  let planRejected=false;
  try{
   const result=await post('/api/backup/start',{destination:$('backupDestination').value,password:$('backupPassword').value,allow_unprotected:$('backupUnprotected').checked,confirm_stop:true,...backupOptions(selectedMethod())});
   clearInterval(timer);
   dialog.querySelector('#runAbout').textContent='FamilyFoto is stopped while the backup runs. Keep the backup disk connected.';
   startProgress(dialog,{expectedRun:result.run});
  }catch(error){
   dialog.querySelector('#runProgress').hidden=true;
   if(error.backupPlanRejected===true){
    planRejected=true;
    dialog.querySelector('#runState').textContent='The backup did not start.';
    dialog.querySelector('#runAbout').textContent='FamilyFoto is still running.';
    const problem=dialog.querySelector('#runError');problem.hidden=false;
    const list=document.createElement('ul');
    for(const message of Array.isArray(error.problems)?error.problems:[error.message]){const item=document.createElement('li');item.textContent=message;list.append(item);}
    problem.replaceChildren(list);
    dialog.querySelector('#runDone').hidden=false;
    dialog.querySelector('#runDoneText').textContent='Correct the settings and try again.';
    const back=document.createElement('button');back.type='button';back.className='btn btn-primary';back.textContent='Return to settings';
    back.onclick=()=>{confirmation.close();pending=false;controls();status('The backup did not start. Correct the settings and try again.');loadRestores().catch(()=>{});};
    dialog.querySelector('#runReturn').replaceWith(back);
    return;
   }
   dialog.querySelector('#runState').textContent='Backup start could not be confirmed.';
   const problem=dialog.querySelector('#runError');problem.hidden=false;problem.textContent=error.message;
   dialog.querySelector('#runDone').hidden=false;
   dialog.querySelector('#runDoneText').textContent='Check backup status before trying to start another backup.';
   const link=dialog.querySelector('#runReturn');link.href='/backup-progress';link.textContent='Open backup status';
  }finally{clearInterval(timer);if(!planRejected)$('backupPassword').value='';}
 };
 return {load};
}
