const messages={starting:'Starting the backup…',['stopping-app']:'Stopping FamilyFoto…',capturing:'Backing up…',verifying:'Checking every file in the finished backup…',failed:'The backup did not finish.',restarting:'Restarting FamilyFoto…',finished:'Finished.',none:'No backup is running.'};
function bytes(value){const units=['B','KB','MB','GB','TB'];let index=0;while(value>=1024&&index<units.length-1){value/=1024;index+=1;}return `${value.toFixed(index?1:0)} ${units[index]}`;}
export function progressText(progress){
 if(!progress)return '';
 const mediaPhase={'media-preparation':'Preparing media copy','media-transfer':'Copying photos and videos','media-verification':'Verifying copied media','media-final-check':'Checking final media consistency'}[progress.phase];
 if(mediaPhase){
  const counts=progress.total_files===undefined?'':` - ${progress.files} of ${progress.total_files} files, ${bytes(progress.bytes)} of ${bytes(progress.total_bytes)}`;
  return `${mediaPhase}: ${progress.state||'running'}, ${(progress.elapsed_seconds||0).toFixed(1)} seconds${counts}`;
 }
 if(progress.phase==='worker-exited')return 'Media worker exited cleanly; finishing the backup…';
 if(progress.phase==='worker-exit')return 'Waiting for the media worker to finish shutting down…';
 if(progress.phase==='sample-verify')return `Checking sample: ${progress.files} of ${progress.total_files} selected files`;
 if(progress.phase==='copy-and-verify')return progress.total_files===undefined?'Copying and checking photos and videos…':`Copying and checking ${progress.files} of ${progress.total_files} files, ${bytes(progress.bytes)} of ${bytes(progress.total_bytes)}`;
 if(progress.phase==='verify')return `Checking ${progress.files} of ${progress.total_files} files, ${bytes(progress.bytes)} of ${bytes(progress.total_bytes)}`;
 if(progress.phase==='copy')return `Copying ${progress.files} of ${progress.total_files} files, ${bytes(progress.bytes)} of ${bytes(progress.total_bytes)}`;
 if(progress.phase==='inventory')return `Reading the library: ${progress.files} files, ${bytes(progress.bytes)}`;
 if(progress.phase==='inventory-verify')return `Rechecking the library: ${progress.entries} entries`;
 if(progress.phase==='database')return `Saving the catalog: ${bytes(progress.bytes)}`;
 if(progress.phase==='state')return `Saving application data: ${bytes(progress.bytes)}`;
 const preparation={'thumbnails-inventory':'Finding thumbnails','thumbnails':'Saving thumbnails',
  'thumbnails-verify':'Checking saved thumbnails','copy-source-check':'Checking media before copying',
  'copy-paths':'Checking destination paths','copy-inventory-check':'Checking the copy inventory',
  'copy-space':'Checking remaining copy space','copy-folders':'Preparing destination folders',
  'media-inventory':'Reading the media inventory','catalog':'Saving the catalog',
  'media-copy':'Copying media','publication':'Completing the backup'}[progress.phase];
 if(preparation)return `${preparation}: ${progress.entries!==undefined?`${progress.entries} entries`:bytes(progress.bytes||0)}`;
 return '';
}
export function progressPercent(progress){
 if(['copy','verify','copy-and-verify','media-transfer','media-verification'].includes(progress?.phase)&&progress.total_bytes>0)return Math.min(100,progress.bytes/progress.total_bytes*100);
 return null;
}
export function finishedText(run){
 if(run.restart_error)return `${run.restart_error}. Start it with the FamilyFoto server control.`;
 if(run.verified_at)return 'Backup finished and verified.';
 if(run.gave_up)return 'The backup did not finish, so this folder is not a recovery point.';
 return 'Finished.';
}
// The page keeps the end of a long log, which is the part that says what is happening now.
export const LOG_KEEP=200000;
export function appendLog(text,more,keep=LOG_KEEP){
 const joined=text+more;
 if(joined.length<=keep)return joined;
 const cut=joined.indexOf('\n',joined.length-keep);
 return cut<0?joined.slice(-keep):joined.slice(cut+1);
}
export function returnShown(run){return Boolean(run.finished&&run.authorized&&!run.restart_error);}
export function retainedAttempts(run){
 if(!run.authorized||!Array.isArray(run.retained_attempts))return [];
 return [...new Set(run.retained_attempts.filter(path=>typeof path==='string'&&path.length>0))];
}
export function startProgress(root=document,{expectedRun=null}={}){
const $=id=>root.querySelector(`#${id}`);
let logNext=0,logText='',stopped=false,timer=null,readingLog=false;
async function readLog(){
 if(!$('runLog').open||readingLog)return;
 readingLog=true;
 try{
  const response=await fetch(`/api/backup-run/log?after=${logNext}`,{cache:'no-store',signal:AbortSignal.timeout(10000)});
  if(!response.ok)return;
  const chunk=await response.json();
  if(chunk.next<logNext){logNext=0;logText='';}
  if(!chunk.text){logNext=chunk.next;return;}
  const view=$('runLogText'),atEnd=view.scrollTop+view.clientHeight>=view.scrollHeight-8;
  logText=appendLog(logText,chunk.text);logNext=chunk.next;view.textContent=logText;
  if(atEnd)view.scrollTop=view.scrollHeight;
 }catch{/* The next refresh tries again. */}finally{readingLog=false;}
}
function show(run){
 $('runState').textContent=run.state==='finished'&&run.authorized?finishedText(run):messages[run.state]||run.state;
 const percent=run.authorized?progressPercent(run.progress):null;
 $('runProgress').hidden=percent===null;if(percent!==null)$('runProgress').value=percent;
 $('runDetail').textContent=run.authorized?progressText(run.progress):'Only the browser that started this backup can see its details.';
 $('runError').hidden=!run.error||!run.authorized;$('runError').textContent=run.error||'';
 $('runDecision').hidden=run.state!=='failed'||!run.authorized;
 $('runRetry').textContent=run.copy_strategy==='fast-full'?'Start new attempt':'Try again';
 $('runDone').hidden=!returnShown(run);
 $('runDoneText').textContent=run.gave_up?'FamilyFoto is running again.':'FamilyFoto is running again, with the backup safely on the backup disk.';
 $('runAbout').hidden=Boolean(run.finished);
 $('runLog').hidden=!run.authorized;
 $('runFolder').textContent=run.directory?`Backup folder: ${run.directory}`:'';
 const retained=retainedAttempts(run);
 $('runRetained').hidden=retained.length===0;
 $('runRetainedFolders').replaceChildren(...retained.map(path=>{const item=document.createElement('li');item.textContent=path;return item;}));
}
async function refresh(){
 if(stopped||!root.isConnected)return;
 try{
  const response=await fetch('/api/backup-run',{cache:'no-store',signal:AbortSignal.timeout(10000)});
  if(response.status===401){$('runState').textContent='FamilyFoto is back. Sign in to continue.';$('runDoneText').textContent='';$('runReturn').textContent='Sign in to FamilyFoto';$('runDone').hidden=false;return;}
  if(response.ok){
   const run=await response.json();
   if(expectedRun&&run.authorized&&run.id!==expectedRun){
    $('runDetail').textContent='Waiting for this backup to become available…';
   }else{show(run);await readLog();}
  }else{$('runDetail').textContent='Waiting for the backup process to respond…';}
 }catch{$('runDetail').textContent='Waiting for FamilyFoto to respond…';}
 if(!stopped)timer=setTimeout(refresh,1500);
}
async function decide(action){
 $('runRetry').disabled=$('runRestart').disabled=true;
 try{
  const response=await fetch('/api/backup-run/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action}),signal:AbortSignal.timeout(10000)});
  if(!response.ok){$('runError').hidden=false;$('runError').textContent=(await response.json()).error||'That did not work.';}
 }catch{$('runError').hidden=false;$('runError').textContent='The decision could not be confirmed. Check the latest status before trying again.';
 }finally{$('runRetry').disabled=$('runRestart').disabled=false;}
}
 $('runRetry').onclick=()=>decide('retry');$('runRestart').onclick=()=>decide('restart');
 $('runLog').addEventListener('toggle',()=>readLog());refresh();
 return ()=>{stopped=true;clearTimeout(timer);};
}
if(typeof document!=='undefined'&&document.getElementById('runState'))startProgress();

export function mountProgressDialog(dialog){
 dialog.classList.add('backup-progress','backup-progress-dialog');
 dialog.setAttribute('aria-labelledby','runTitle');dialog.removeAttribute('aria-describedby');
 dialog.innerHTML=`<h2 id="runTitle">FamilyFoto backup</h2>
 <p id="runAbout" class="muted">FamilyFoto is stopped while the backup runs. Keep the backup disk connected.</p>
 <p id="runState" role="status" aria-live="polite" tabindex="-1">Starting backup…</p>
 <progress id="runProgress" max="100" hidden></progress><p id="runDetail" class="muted"></p>
 <p id="runError" class="notice" hidden></p>
 <div id="runDecision" hidden><p>The library stays locked until you choose. Nothing has been deleted.</p><button id="runRetry" type="button">Try again</button> <button id="runRestart" type="button">Give up and restart FamilyFoto</button></div>
 <div id="runDone" hidden><p id="runDoneText"></p><a id="runReturn" class="btn btn-primary" href="/">Return to FamilyFoto</a></div>
 <p id="runFolder" class="muted"></p>
 <section id="runRetained" hidden><h3>Retained incomplete attempts</h3><ul id="runRetainedFolders"></ul></section>
 <details id="runLog" hidden><summary>Show the backup log</summary><pre id="runLogText" tabindex="0" aria-live="off"></pre></details>`;
 dialog.querySelector('#runState').focus();
}
