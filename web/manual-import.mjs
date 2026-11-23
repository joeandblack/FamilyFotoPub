import {confirmAction} from './confirm-dialog.mjs';
import {setupImportStorage} from './import-storage.mjs';
export function setupManualImport({api,notice,refresh}){
  const section=document.createElement('section');section.id='manualImport';
  section.innerHTML='<label for="importSource">Source folder on this computer</label><div class="manual-source"><input id="importSource" type="text" placeholder="Full path to your source folder" autocomplete="off"><button id="browseImport" title="Select source folder" aria-label="Select source folder"><i data-lucide="folder-open"></i></button><button id="startImport"><i data-lucide="play"></i> Start</button></div><small role="status">Copy from the selected source; source files remain unchanged</small>';
  document.getElementById('imports').prepend(section);
  section.classList.add('admin-storage');
  const disclosure=document.createElement('details'),entry=document.createElement('summary');
  entry.textContent='Select a server folder to import';
  disclosure.append(entry);
  const server=document.createElement('div');server.id='serverImport';
  while(section.firstChild)server.append(section.firstChild);
  disclosure.append(server);section.append(disclosure);
  const startError=document.createElement('p');startError.id='importStartError';startError.className='pipeline-error';startError.setAttribute('role','alert');startError.hidden=true;section.append(startError);
  const storage=setupImportStorage(disclosure,api);
  const options=document.createElement('div');options.className='pipeline-options';
  options.innerHTML='<label><input id="newAutoMetadata" type="checkbox" checked> Process videos after import</label><label><input id="newAutoConversion" type="checkbox"> Convert AVI / FLV / MTS after video processing</label><label><input id="newAutoDuplicates" type="checkbox"> Photo duplicate scan</label>';
  disclosure.append(options);
  const $=id=>document.getElementById(id);let busy=false,dirty=false,active=true,launchingUntil=0;
  const sync=()=>{$('startImport').disabled=busy||active||Date.now()<launchingUntil;};
  sync();
  $('importSource').oninput=()=>{dirty=true;};
  const selectionStatus=server.querySelector('small');selectionStatus.setAttribute('role','status');
  async function browse(){
    if(busy)return;busy=true;$('browseImport').disabled=$('startImport').disabled=true;
    selectionStatus.textContent='Waiting for folder selection on this computer...';
    try{
      const result=await api('/api/import/select-folder',{method:'POST'});
      if(!result.cancelled){$('importSource').value=result.path;dirty=true;}
      selectionStatus.textContent=result.cancelled?'Selection cancelled':'Copy from the selected source; source files remain unchanged';
    }catch(error){selectionStatus.textContent=error.message;}finally{busy=false;$('browseImport').disabled=false;sync();}
  }
  $('browseImport').onclick=()=>browse();
  $('startImport').onclick=async()=>{
    if(busy||active||Date.now()<launchingUntil)return;
    const source=$('importSource').value.trim();if(!source){$('importSource').focus();return;}
    if(!await confirmAction(`Start importing from ${source}? Exact duplicates will be skipped. Source files remain unchanged.`,{title:'Start import?',confirmLabel:'Start import'}))return;
    if(busy||active||Date.now()<launchingUntil)return;
    busy=true;startError.hidden=true;startError.textContent='';sync();
    selectionStatus.textContent='Checking disks and starting import...';
    try{
      await storage.ensureDestination(source);
      const result=await api('/api/import/start',{method:'POST',body:JSON.stringify({source,auto_metadata:$('newAutoMetadata').checked,auto_conversion:$('newAutoConversion').checked,auto_duplicates:$('newAutoDuplicates').checked})});dirty=false;launchingUntil=Date.now()+15000;disclosure.open=false;selectionStatus.textContent=result.message;notice(result.message);await refresh();}
    catch(error){selectionStatus.textContent='Import not started';startError.textContent=error.message;startError.hidden=false;disclosure.open=true;startError.scrollIntoView({block:'nearest'});}finally{busy=false;sync();}
  };
  return {setStorage:storage.setStorage,setActivity(pipeline){active=!!pipeline?.busy||pipeline?.handoff?.state==='pending';if(active)launchingUntil=0;sync();},setDefault(source){if(!dirty&&source)$('importSource').value=source;}};
}
