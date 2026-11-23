const GIB=1024**3,MIB=1024**2;
export const FIELDS=[
 {key:'max_files',label:'Files per upload',unit:'files',scale:1},
 {key:'max_file_bytes',label:'Largest single file',unit:'GiB',scale:GIB},
 {key:'max_total_bytes',label:'Total size of one upload',unit:'GiB',scale:GIB},
 {key:'staging_bytes',label:'Space for uploads waiting to import',unit:'GiB',scale:GIB},
 {key:'reserve_bytes',label:'Free disk space to keep',unit:'GiB',scale:GIB},
 {key:'chunk_bytes',label:'Transfer piece size',unit:'MiB',scale:MIB},
 {key:'expiry_hours',label:'Abandon unfinished uploads after',unit:'hours',scale:1},
 {key:'upload_mibps',label:'Transfer speed limit',unit:'MiB/s',scale:1}];
const round=value=>Math.round(value*1000)/1000;
export const toDisplay=(field,value)=>round(value/field.scale);
export function fromForm(raw){
 const values={};
 for(const field of FIELDS){
  const number=Number(raw[field.key]);
  if(!Number.isFinite(number))throw new Error(`${field.label}: enter a number`);
  values[field.key]=Math.round(number*field.scale);
 }
 return values;
}
export function setupUploadSettings({api}){
 const section=document.createElement('details');section.id='uploadSettings';
 section.className='settings-section';
 section.innerHTML=`<summary>Upload limits</summary>
 <p>Applies to files sent from other computers. New limits apply to uploads started afterwards.</p>
 <div id="uploadSettingsFields"></div>
 <button id="saveUploadSettings" type="button">Save upload limits</button>
 <button id="resetUploadSettings" type="button">Restore defaults</button>
 <p id="uploadSettingsStatus" role="status"></p>`;
 document.getElementById('storage').prepend(section);
 const $=id=>document.getElementById(id);let data=null,dirty=false,pending=false;
 const status=text=>{$('uploadSettingsStatus').textContent=text;};
 const grid=$('uploadSettingsFields');
 for(const field of FIELDS){
  const label=document.createElement('label');label.htmlFor='uploadSetting_'+field.key;label.textContent=`${field.label} (${field.unit})`;
  const input=document.createElement('input');input.id='uploadSetting_'+field.key;input.type='number';input.step='any';input.min='0';
  input.oninput=()=>{dirty=true;};
  grid.append(label,input);
 }
 const controls=()=>{$('saveUploadSettings').disabled=pending||!data;$('resetUploadSettings').disabled=pending||!data;};
 function show(){
  for(const field of FIELDS){
   const bounds=data.bounds[field.key];
   const input=$('uploadSetting_'+field.key);
   input.value=toDisplay(field,data.values[field.key]);
   input.title=`Allowed ${toDisplay(field,bounds[0])} to ${toDisplay(field,bounds[1])} ${field.unit}; default ${toDisplay(field,data.defaults[field.key])}`;
  }
 }
 async function send(method,body){
  pending=true;controls();
  try{data=await api('/api/upload-settings',{method,...(body?{body:JSON.stringify(body)}:{})});dirty=false;show();status(method==='DELETE'?'Defaults restored.':'Saved.');}
  catch(error){status(error.message);}finally{pending=false;controls();}
 }
 $('saveUploadSettings').onclick=()=>{
  const raw={};for(const field of FIELDS)raw[field.key]=$('uploadSetting_'+field.key).value;
  try{send('PUT',fromForm(raw));}catch(error){status(error.message);}
 };
 $('resetUploadSettings').onclick=()=>send('DELETE');
 async function load(){
  section.hidden=document.body.dataset.role!=='admin';if(section.hidden)return;
  try{data=await api('/api/upload-settings');if(!dirty)show();status('');}
  catch(error){status(error.message);}finally{controls();}
 }
 return {load};
}
