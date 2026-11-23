import {setupUploads} from './uploads.mjs';
// The Uploads page: files sent from another computer by a signed-in person.
// Server-folder copying by an administrator stays on the Imports page.
export function setupUploadsView({api,notice,refresh}){
  const section=document.getElementById('uploads');
  section.innerHTML='<section id="newUpload"><h3>New upload</h3><p class="muted">Send a folder of photos and videos from this computer. Limits are set by an administrator.</p><div id="uploadPicker"></div><fieldset id="uploadVisibility"><legend>Who can see these photos and videos</legend><label><input type="radio" name="uploadVisibility" value="shared" checked> Everyone with an account</label><label><input type="radio" name="uploadVisibility" value="private"> Only me and administrators</label></fieldset><div class="pipeline-options"><label><input id="uploadAutoMetadata" type="checkbox" checked> Process videos after upload</label><label><input id="uploadAutoConversion" type="checkbox"> Convert AVI / FLV / MTS after video processing</label><label><input id="uploadAutoDuplicates" type="checkbox"> Photo duplicate scan</label></div></section><div id="uploadHistory"></div>';
  const checked=id=>section.querySelector('#'+id).checked;
  const uploads=setupUploads(section.querySelector('#uploadPicker'),{notice,refresh,api,
    options:()=>({visibility:'shared',auto_metadata:checked('uploadAutoMetadata'),auto_conversion:checked('uploadAutoConversion'),auto_duplicates:checked('uploadAutoDuplicates')}),
    beforeStart:async()=>{
      if(document.body.dataset.role!=='admin')return;
      const settings=await api('/api/storage/settings');
      if(!settings.destination)throw Error('Choose the library destination in Settings before the first upload.');
    }});
  return {history:section.querySelector('#uploadHistory'),update:uploads.update};
}
