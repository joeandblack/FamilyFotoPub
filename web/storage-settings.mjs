export function setupStorageSettings({api,refresh}){
 const section=document.createElement('details');section.id='storageSettings';
 section.className='settings-section';
 section.innerHTML=`<summary>Library storage</summary>
 <label for="storageDestination">Library destination</label>
 <div class="manual-source"><input id="storageDestination" autocomplete="off"><button id="browseStorageDestination" type="button">Browse</button></div>
 <p id="storageDestinationHint"></p><button id="saveStorageSettings" type="button">Save destination settings</button>
 <p id="storageSettingsStatus" role="status"></p>`;
 document.getElementById('storage').prepend(section);
 const $=id=>document.getElementById(id);let data=null,dirty=false,pending=false;
 const status=text=>{$('storageSettingsStatus').textContent=text;};
 $('storageDestination').oninput=()=>{dirty=true;};
 function controls(){
  $('saveStorageSettings').disabled=pending||!data||data.busy;
  $('storageDestination').disabled=pending||!data||data.destination_locked;
  $('browseStorageDestination').disabled=$('storageDestination').disabled;
 }
 async function load(){
  section.hidden=document.body.dataset.role!=='admin';if(section.hidden)return;
  try{
   data=await api('/api/storage/settings');
   if(!dirty){$('storageDestination').value=data.destination;}
   $('storageDestinationHint').textContent=data.destination_locked?'This library already has imports or uploads. Its destination is locked; moving it requires a verified migration. No media will be moved by these settings.':'For an empty library, choose a new or empty folder on a local disk. No files are moved.';
   status(data.busy?'Media work is active. Settings can be saved after it finishes or is paused.':'Existing imports keep their original source paths and disk identities.');
  }catch(error){status(error.message);}finally{controls();}
 }
 async function browse(id){
  pending=true;controls();status('Choose a folder on the FamilyFoto computer…');
  try{const result=await api('/api/storage/select-folder',{method:'POST'});if(!result.cancelled){$(id).value=result.path;dirty=true;}status(result.cancelled?'Selection cancelled':'Folder selected. Save to apply; no import will start.');}
  catch(error){status(error.message);}finally{pending=false;controls();}
 }
 $('browseStorageDestination').onclick=()=>browse('storageDestination');
 $('saveStorageSettings').onclick=async()=>{
  pending=true;controls();
  try{const result=await api('/api/storage/settings',{method:'POST',body:JSON.stringify({source:data.source,destination:$('storageDestination').value.trim(),revision:data.revision})});dirty=false;await refresh();status(result.message);}
  catch(error){status(error.message);}finally{pending=false;controls();}
 };
 return {load};
}
