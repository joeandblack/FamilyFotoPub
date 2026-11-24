import './session-activity.mjs';
import {backupActive} from './backup-state.mjs';
import {buildLabel,buildTitle} from './build-label.mjs';
import {isAudio} from './audio-media.mjs';
import {showNotice} from './confirm-dialog.mjs';
import {createPeopleStartup} from '../face_detect/web/people-startup.mjs';
import {renderImportOverview} from './import-overview.mjs';
import {setupImportDiagnostics} from './import-diagnostics.mjs';
import {setupConversionReview} from './conversion-review.mjs';
const reviewPlayback=setupConversionReview({api,openViewer:(...args)=>openViewer(...args)});
const reviewImportDiagnostics=setupImportDiagnostics(api);
import {createLoadingStatus} from './loading-status.mjs';
import {setupAlbums} from './albums.mjs';
import {apiErrorMessage} from './api-error.mjs';
import { createEstimateDisplay } from './progress.mjs';
import {operationStatus,operationIndicator} from './operation-status.mjs';
import { readNavigation, saveNavigation, importNavigation, viewForRole } from './navigation.mjs';
import { updateGallery } from './gallery.mjs';
import {createPeoplePhotoGallery} from '../face_detect/web/people-photo-gallery.mjs';
import { setupViewer } from './viewer.mjs';
import {size} from './size.mjs';
import {videoDuration} from './duration.mjs';
import {setupTrashSelection} from './trash-selection.mjs';
import {setupPeopleOrder} from '../face_detect/web/people-order.mjs';
import {pageSizeControl} from './page-size.mjs';
import {setupManualImport} from './manual-import.mjs';
import {setupUploadsView} from './uploads-view.mjs';
import {renderPipelines,updateImportPeopleStatus} from './import-pipelines.mjs';
import {createImportDetailLoader} from './import-detail-loader.mjs';
const loadImportDetails=createImportDetailLoader(api);
import {setupStorageSettings} from './storage-settings.mjs';
import {setupUploadSettings} from './upload-settings.mjs';
import {setupBackupSettings} from './backup-settings.mjs';
import {setupFaceService} from '../face_detect/web/face-service.mjs';
import {setupAccountSecurity} from './account-security.mjs';
import {setupUserManagement} from './user-management.mjs';
import {setupPhotoDuplicateReview} from './photo-duplicates.mjs';
const reviewDuplicates=setupPhotoDuplicateReview(api);
const $=id=>document.getElementById(id);
const loadingElement=document.createElement('div');loadingElement.id='pageLoading';loadingElement.hidden=true;loadingElement.setAttribute('role','status');loadingElement.setAttribute('aria-live','polite');
document.querySelector('main > header').after(loadingElement);
const pageLoading=createLoadingStatus(loadingElement);let finishPeopleLoading=()=>{},peopleResultsReady=false,peopleAwaitingUnfiltered=false;
const overviewPanel=document.createElement('section');overviewPanel.id='importOverview';overviewPanel.hidden=true;
const overviewTable=document.createElement('table');overviewTable.setAttribute('aria-label','Import overview');
overviewPanel.tabIndex=0;overviewPanel.setAttribute('aria-label','Scrollable import overview');overviewPanel.append(overviewTable);document.querySelector('main').append(overviewPanel);
const overviewButton=document.createElement('button');overviewButton.className='btn btn-sm';overviewButton.hidden=true;
overviewButton.onclick=()=>{view=view==='import-overview'?'imports':'import-overview';showView();};
document.querySelector('main > header').append(overviewButton);
let overviewLoading=false,overviewRevision=0;
async function loadOverview(){
 if(overviewLoading)return;overviewLoading=true;const ticket=++overviewRevision;
 const finish=overviewTable.children.length?()=>{}:pageLoading.start('Loading imports...');
 try{
  void refreshPeopleStatus();
  const history=await api('/api/import/history'),peopleStatus=peopleStartup.merge(previewImportStatus||{});
  if(view!=='import-overview'||ticket!==overviewRevision){finish();return;}
  const serverImports=history.imports.filter(job=>job.source_kind!=='upload');renderImportOverview(overviewTable,serverImports,history.pipeline,peopleStatus,reviewImportDiagnostics);$('summary').textContent=`${serverImports.length} imports`;
  finish();
 }catch(error){finish(error.message);}finally{overviewLoading=false;}
}
const historyContainer=document.createElement('div');historyContainer.id='importHistory';
const ownershipReview=document.createElement('p');ownershipReview.id='ownershipReview';ownershipReview.className='pipeline-error';ownershipReview.hidden=true;
$('imports').append(ownershipReview,historyContainer);
let historyUpdated=0,historyLoading=false,historyCommand=false,historyRevision=0;
setInterval(()=>{if(view==='import-overview')loadOverview();},5000);
let previewImportStatus=null;
const peopleStartup=createPeopleStartup({request:fetch});
let peopleStatusLoading=false;
async function refreshPeopleStatus(){
 if(backupActive()||peopleStatusLoading||previewNav.hidden)return;
 peopleStatusLoading=true;
 try{previewImportStatus=await api('/api/people-preview/import-status');updateImportPeopleStatus(historyContainer,peopleStartup.merge(previewImportStatus));if(latest)renderStatus(latest);}
 catch{/* Retain the last known scan state while People is busy. */}
 finally{peopleStatusLoading=false;}
}
async function previewImportCommand(id,action,button){
 button.disabled=true;
 const row=button.closest('.pipeline-stage');
 await peopleStartup.command(id,action,()=>{
  const state=peopleStartup.merge(previewImportStatus||{})[id];
  updateImportPeopleStatus(historyContainer,peopleStartup.merge(previewImportStatus||{}));
  if(row?.isConnected){const label=row.querySelector('.pipeline-heading span');if(label)label.textContent=state?.state==='preparing'?'Preparing scan':state?.state||'Checking status';let message=row.querySelector('.people-startup-message');if(!message){message=document.createElement('p');message.className='people-startup-message muted';message.setAttribute('role','status');row.append(message);}message.textContent=state?.message||'';}
  void loadImportHistory(true);
 });
 void refreshPeopleStatus();
}
function reviewImportPeople(id){peopleAwaitingUnfiltered=false;peopleResultsReady=false;previewPanel.style.visibility='hidden';previewPanel.src=`/people-preview?desktop=1&jobs=1&integrated=1&import=${id}#review-faces`;view='people-preview';showView();}
async function groupCommand(id,action,button){
 historyRevision++;historyCommand=true;button.disabled=true;
 try{const result=await api(`/api/import/${id}/${action}`,{method:'POST'});notice(result.message);}
 catch(error){await showNotice(error.message);}
 finally{historyCommand=false;await loadImportHistory(true);}
}
async function groupOptions(id,options,input){
 historyRevision++;historyCommand=true;
 for(const control of input.closest('.import-group').querySelectorAll('.pipeline-options input'))control.disabled=true;
 try{await api(`/api/import/${id}/options`,{method:'POST',body:JSON.stringify(options)});}
 catch(error){await showNotice(error.message);}
 finally{historyCommand=false;await loadImportHistory(true);}
}
async function trashOptions(id,enabled,input){
 historyRevision++;historyCommand=true;input.disabled=true;
 try{await api(`/api/import/${id}/photo-duplicates/auto-trash`,{method:'POST',body:JSON.stringify({enabled})});}
 catch(error){await showNotice(error.message);}
 finally{historyCommand=false;await loadImportHistory(true);}
}
async function loadImportHistory(force=false){
 if(historyLoading||historyCommand||(!force&&Date.now()-historyUpdated<4500))return;
 historyLoading=true;const revision=historyRevision;
 void refreshPeopleStatus();
 try{
  const result=await api('/api/import/history?summary=true');if(revision!==historyRevision)return;
  ownershipReview.hidden=!result.ownership_review_count;
  ownershipReview.textContent=result.ownership_review_count?`${result.ownership_review_count} legacy videos need original-import ownership review and are excluded from post-processing.`:'';
  manualImport.setDefault(result.imports.filter(job=>job.state!=='cancelled'&&job.source_kind!=='upload').sort((a,b)=>b.id-a.id)[0]?.source_root);
  const callbacks={command:groupCommand,options:groupOptions,loadDetails:loadImportDetails,viewLibrary,reviewDuplicates,reviewIssues:reviewImportDiagnostics,reviewPlayback,trashOptions,peopleReview:previewNav.hidden?null:{status:peopleStartup.merge(previewImportStatus||{}),command:previewImportCommand,review:reviewImportPeople},createAlbum:id=>albums.fromImport(id)};
  renderPipelines(historyContainer,result.imports.filter(job=>job.source_kind!=='upload'),result.pipeline,callbacks);
  renderPipelines(uploadsView.history,result.imports.filter(job=>job.source_kind==='upload'),result.pipeline,{...callbacks,organizeSections:false,emptyText:'No uploads yet.'});
  lucide.createIcons();historyUpdated=Date.now();if(view==='uploads')uploadsView.update();
 }
 catch(error){notice(error.message);}
 finally{historyLoading=false;if(revision!==historyRevision&&!historyCommand)await loadImportHistory(true);}
}
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let albumId=null,collectionId=null,albumCollection=null;
let page=1,libraryPageSize=1000,view='all',items=[],latest=null,request=0,lastGalleryRefresh=0;
const librarySizeControl=pageSizeControl(libraryPageSize,value=>{libraryPageSize=value;page=1;load();},'Library items per page');
document.querySelector('#library footer>div').append(librarySizeControl);
const estimateDisplay=createEstimateDisplay();
const albums=setupAlbums({api,openAlbum,openCollection,openPerson:(cid,person)=>openPeoplePhotos(null,{url:`/api/album-collections/${cid}/people/${person.person_id}/photos?pilot=${encodeURIComponent(person.pilot)}`,name:person.name}),refresh:()=>load(true,true),notice});
const trashSelection=setupTrashSelection({api,refresh:()=>load(true,true),albumAction:(action,ids)=>albums.action(action,ids)});
setupPeopleOrder($('grid'),async(id,targetId,after)=>{
 const aid=albumId,ticket=request,grid=$('grid');
 const finish=pageLoading.start('Saving photo order...');grid.inert=true;
 try{
  const result=await api(`/api/albums/${aid}/items/${id}/position`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_id:targetId,after})});
  if(albumId===aid&&request===ticket){
   const positions=new Map(result.order.map((value,index)=>[value,index]));
   items.sort((a,b)=>positions.get(a.id)-positions.get(b.id));
   const nodes=new Map([...grid.querySelectorAll('[data-id]')].map(node=>[Number(node.dataset.id),node]));
   for(const item of items)if(nodes.has(item.id))grid.append(nodes.get(item.id));
  }
  finish();
 }catch(error){finish(error.message);}finally{grid.inert=false;}
},{selector:'.tile',idKey:'id',enabled:()=>view==='albums'&&albumId!==null&&!$('grid').classList.contains('selection-mode')&&$('grid').getAttribute('aria-busy')!=='true'});
const saved=readNavigation(sessionStorage);page=saved.page;view=saved.view;
$('search').value=saved.search;
if(!['all','unknown'].includes(saved.year))$('year').add(new Option(saved.year,saved.year));
$('year').value=saved.year;
if(saved.import_job!=='all')$('importFilter').add(new Option('Import #'+saved.import_job,saved.import_job));
$('importFilter').value=saved.import_job;
function remember(){saveNavigation(sessionStorage,{view,page,year:$('year').value,import_job:$('importFilter').value,search:$('search').value});}
const previewNav=document.createElement('button');previewNav.textContent='People';previewNav.dataset.view='people-preview';previewNav.hidden=true;
document.querySelector('nav [data-view="imports"]').before(previewNav);
const previewPanel=document.createElement('iframe');previewPanel.title='People preview';previewPanel.hidden=true;
previewPanel.style.cssText='width:100%;flex:1;min-height:0;border:0';document.querySelector('main').append(previewPanel);
const peopleToolbar=document.createElement('div');peopleToolbar.id='peopleToolbar';peopleToolbar.hidden=true;
peopleToolbar.setAttribute('role','toolbar');peopleToolbar.setAttribute('aria-label','People actions');
let peopleReviewOpen=false;
function renderPeopleHeading(){
 $('heading').replaceChildren(document.createTextNode('People'));
 if(peopleReviewOpen){const subtitle=document.createElement('span');subtitle.className='page-subtitle';subtitle.textContent=' / Review faces';$('heading').append(subtitle);}
}
const peopleBack=document.createElement('button');peopleBack.type='button';peopleBack.className='btn btn-sm';peopleBack.textContent='Back to People';peopleBack.hidden=true;
function openPeopleHome(){
 peopleReviewOpen=false;peopleResultsReady=false;peopleAwaitingUnfiltered=true;
 peopleBack.hidden=true;previewPanel.style.visibility='hidden';
 peopleToolbar.querySelector('[data-people-action="review-faces"]').hidden=false;
 renderPeopleHeading();finishPeopleLoading=pageLoading.start('Loading people...');
 // Navigation must not depend on a busy or older review-frame message handler.
 previewPanel.src='/people-preview?desktop=1&jobs=1&integrated=1';
}
peopleBack.onclick=openPeopleHome;
for(const [action,label,icon] of [['refresh','Refresh','refresh-cw'],['review-faces','Review faces','scan-face'],['ignored-faces','Ignored faces','user-x'],['blurry-faces','Blurry faces','focus']]){
 const button=document.createElement('button');button.type='button';button.className='btn btn-sm';button.disabled=true;button.title=label;button.setAttribute('aria-label',label);
 const glyph=document.createElement('i');glyph.dataset.lucide=icon;button.append(glyph);
 if(action!=='refresh')button.append(document.createTextNode(label));
 button.dataset.peopleAction=action;
 button.onclick=()=>{
  if(action==='refresh'){button.disabled=true;button.setAttribute('aria-busy','true');button.title='Refreshing people...';}
  previewPanel.contentWindow?.postMessage({type:'familyfoto-people-action',action},location.origin);
 };
 peopleToolbar.append(button);
}
peopleToolbar.append(peopleBack);
previewPanel.addEventListener('load',()=>previewPanel.contentWindow?.postMessage({type:'familyfoto-people-ready-request'},location.origin));
window.addEventListener('message',event=>{
 if(event.origin!==location.origin||event.source!==previewPanel.contentWindow)return;
 if(event.data?.type==='familyfoto-people-view'){
  peopleReviewOpen=!!event.data.review;peopleBack.hidden=!peopleReviewOpen;
  peopleToolbar.querySelector('[data-people-action="review-faces"]').hidden=peopleReviewOpen;
  if(view==='people-preview')renderPeopleHeading();
  return;
 }
 if(event.data?.type==='familyfoto-people-refresh-finished'){
  const button=peopleToolbar.querySelector('[data-people-action="refresh"]');button.disabled=false;button.removeAttribute('aria-busy');button.title='Refresh';return;
 }
 if(event.data?.type==='familyfoto-people-loading'){
  if(peopleAwaitingUnfiltered&&event.data.scope!=null)return;
  peopleResultsReady=!event.data.loading&&!event.data.error;
  if(!event.data.loading){peopleAwaitingUnfiltered=false;previewPanel.style.visibility='';}
  if(view==='people-preview'){
   if(event.data.loading)finishPeopleLoading=pageLoading.start('Loading people...');
   else finishPeopleLoading(event.data.error);
  }
  return;
 }
 if(event.data?.type!=='familyfoto-people-ready')return;
 for(const button of peopleToolbar.querySelectorAll('button'))button.disabled=false;
});
document.querySelector('main > header').append(peopleToolbar);
const galleryToolbar=document.querySelector('.toolbar');
document.querySelector('main > header').append(galleryToolbar);
document.querySelector('.job-progress').append($('importEstimate'));
const privateBadge=m=>m.visibility==='private'?'<span class="private-badge" title="Private: only the owner and administrators can see this">Private</span>':'';
function updateViewLayout(){
 document.body.dataset.view=view;
 peopleToolbar.hidden=view!=='people-preview';
 overviewPanel.hidden=view!=='import-overview';overviewButton.hidden=!['imports','import-overview'].includes(view);
 overviewButton.innerHTML=view==='import-overview'?'<i data-lucide="arrow-left"></i> Back to Imports':'<i data-lucide="table-2"></i> Import overview';lucide.createIcons();
 galleryToolbar.hidden=!(['all','image','video','trash'].includes(view)||(view==='albums'&&albumId!==null));
 $('year').hidden=$('importFilter').hidden=view==='albums';
 if(latest)renderStatus(latest);
}
async function api(url,options){const r=await fetch(url,options);if(r.status===401){if(!backupActive())location.replace('/login');throw Error('Login required');}if(!r.ok){let d=await r.json().catch(()=>({}));const error=Error(apiErrorMessage(d));if(url==='/api/backup/start'&&r.status===409&&d.code==='backup_plan_rejected'){error.backupPlanRejected=true;error.problems=d.problems;}throw error;}return r.json();}
function notice(text){if(backupActive())return;$('notice').textContent=text;$('notice').hidden=!text;}
function fields(target,data){$(target).innerHTML=Object.entries(data).map(([k,v])=>`<dt>${escape(k)}</dt><dd>${escape(v??'Unknown')}</dd>`).join('');}
function runState(job,monitor){if(!job)return 'idle';if(job.state==='paused-error')return 'error';if(['running','scanning'].includes(job.state)){const checked=Date.parse(monitor?.updated);if(monitor?.state!=='healthy'||!Number.isFinite(checked)||Date.now()-checked>60000)return 'error';return 'running';}return 'idle';}
function elapsed(start,seconds){const minutes=Math.max(0,Math.floor((seconds??((Date.now()-Date.parse(start))/1000))/60));return minutes<60?`${minutes}min`:`${Math.floor(minutes/60)}hr ${minutes%60}m`;}
function renderStatus(d){latest=d;const j=d.job;const video=d.video;const conversion=d.conversion;
 const operation=operationStatus({...d,people:previewImportStatus},{peopleReady:previewNav.hidden||previewImportStatus!==null}),active=operation.status;
 const indicator=operationIndicator(operation,d.monitor);
 const videoActive=operation.stage==='metadata',conversionActive=operation.stage==='conversion',duplicateActive=operation.stage==='duplicates';
 const state=indicator.state;for(const id of ['runDot','sideDot']){$(id).className='dot '+state;$(id).title=indicator.title;}
 $('runTime').textContent=state==='running'?operation.label+(operation.stage==='people'?'':' '+elapsed(active.started_at||active.started,active.active_seconds??active.run_seconds)):indicator.label;$('sideState').textContent=$('runTime').textContent;
 const n=j?.processed||0,total=j?.total||0;$('progress').value=total?n/total*100:0;$('progressCount').textContent=`Files: ${n.toLocaleString()} / ${total.toLocaleString()}${total?' - '+(n/total*100).toFixed(1)+'%':''}`;
 const bytes=j?.bytes_by_state||{},done=j?.processed_bytes||0,allBytes=j?.total_bytes||0;
 const estimate=estimateDisplay(j,runState(j,d.monitor)==='running');
 $('importEstimate').hidden=true;
 if(view==='imports'&&state==='running'&&operation.stage==='import')$('runTime').textContent=`Imp. ${elapsed(j.started_at,j.active_seconds)}${estimate?', '+estimate:''}`;
 $('byteProgress').value=allBytes?done/allBytes*100:0;$('byteProgressCount').textContent=`Bytes: ${size(done)} / ${size(allBytes)}${allBytes?' - '+(done/allBytes*100).toFixed(1)+'%':''}`;
 document.querySelector('.job-progress').hidden=true;
 for(const id of ['progress','progressCount','byteProgress','byteProgressCount'])$(id).hidden=!!(videoActive||conversionActive||duplicateActive);
 if(active?.state==='paused-error')notice(active.error);else notice('');
}
async function load(quiet=false,force=false){if(!(['all','image','video','trash'].includes(view)||(view==='albums'&&albumId!==null)))return;trashSelection.context(JSON.stringify([view,albumId,page,libraryPageSize,$('year').value,$('search').value,$('importFilter').value]));remember();if(quiet&&!force&&Date.now()-lastGalleryRefresh<30000)return;lastGalleryRefresh=Date.now();const ticket=++request;
 const finishLoading=(!quiet||!items.length)?pageLoading.start('Loading photos and videos...'):()=>{};
 $('grid').setAttribute('aria-busy','true');$('libraryEmpty').hidden=true;
 try{const d=await api('/api/media?'+new URLSearchParams({page,page_size:libraryPageSize,kind:view==='albums'?'all':view,...(albumId!==null?{album:albumId}:{}),search:$('search').value,year:$('year').value,import_job:$('importFilter').value}));if(ticket!==request)return;page=d.page;remember();items=d.items;$('summary').textContent=`${d.stats.total.toLocaleString()} items - ${size(d.stats.bytes)}`;
 const chosen=$('year').value;const options='<option value="all">All years</option>'+d.years.map(y=>`<option value="${y}">${y}</option>`).join('')+'<option value="unknown">Unknown date</option>';if($('year').innerHTML!==options){$('year').innerHTML=options;$('year').value=chosen;}
const selectedImport=$('importFilter').value;
 const choices=[new Option('All imports','all'),...(d.imports||[]).map(j=>new Option(`${j.source_kind==='upload'?'Upload':'Server'} - ${j.label} #${j.id}`,String(j.id)))];
 if(selectedImport!=='all'&&!choices.some(o=>o.value===selectedImport))choices.push(new Option('Import #'+selectedImport,selectedImport));
 $('importFilter').replaceChildren(...choices);$('importFilter').value=selectedImport;
 $('libraryEmpty').hidden=!!d.total;$('libraryEmpty').textContent=selectedImport==='all'?'No media matches these filters.':`No visible catalog media matches import #${selectedImport} and the current filters. Importing may still be in progress; check Year, media type and search. Trashed items remain in Trash.`;
const scroll=$('grid').scrollTop;const tiles=items.map(m=>{
 const duration=videoDuration(m);
 if(isAudio(m))return {id:String(m.id),html:`<button class="tile audio-tile" data-id="${m.id}" title="${escape(m.name)}" aria-label="${escape(m.name)}, audio${duration?', duration '+duration:''}"><span class="frame"><span class="audio-placeholder"><i data-lucide="music-2" aria-hidden="true"></i><span>Audio</span></span>${duration?`<span class="video-duration" aria-hidden="true">${duration}</span>`:''}</span>${privateBadge(m)}</button>`};
 return {id:String(m.id),html:`<button class="tile" data-id="${m.id}" title="${escape(m.name)}" aria-label="${escape(m.name)}${duration?', duration '+duration:''}"><span class="frame rotation-${m.rotation||0}">${(m.kind==='image'&&!m.metadata_error)||m.video_metadata?.poster?`<img loading="lazy" decoding="async" src="/api/media/${m.id}/thumbnail${m.crop?'?crop='+encodeURIComponent(JSON.stringify(m.crop)):'' }" alt="">`:`<span class="placeholder" aria-hidden="true">${m.kind==='video'?'&#9654;':'&#9671;'}</span>`}${duration?`<span class="video-duration" aria-hidden="true">${duration}</span>`:''}</span>${privateBadge(m)}</button>`};
});updateGallery($('grid'),tiles);
 globalThis.lucide?.createIcons();
 $('grid').scrollTop=quiet?scroll:0;$('previous').disabled=page<=1;$('next').disabled=page>=d.pages;if(document.activeElement!==$('pageNumber'))$('pageNumber').value=page;$('pageNumber').max=d.pages;$('pageCount').textContent=`/ ${d.pages}`;$('range').textContent=d.total?`${(page-1)*d.page_size+1}-${Math.min(page*d.page_size,d.total)} / ${d.total.toLocaleString()}`:'0 items';
 trashSelection.sync(items,view==='albums'?'album':view);
 finishLoading();
 }catch(e){if(ticket===request){finishLoading(e.message);notice(e.message);}}
 finally{if(ticket===request)$('grid').setAttribute('aria-busy','false');else finishLoading();}}
const openViewer=setupViewer({api,fields,size,addToAlbum:id=>albums.add([id]),changed:()=>load(true,true),context:()=>({items,page,pages:Number($('pageNumber').max)||1,filters:{page_size:libraryPageSize,kind:view==='albums'?'all':view,...(albumId!==null?{album:albumId}:{}),search:$('search').value,year:$('year').value,import_job:$('importFilter').value}})});
$('grid').onclick=e=>{const tile=e.target.closest('[data-id]');if(!tile)return;if(trashSelection.toggle(Number(tile.dataset.id),e))return;++request;openViewer(Number(tile.dataset.id));};
let peoplePhotoRequest=0;
const openPeoplePhotos=createPeoplePhotoGallery({api,openViewer});
window.addEventListener('message',async event=>{
 if(event.origin!==location.origin||event.source!==previewPanel.contentWindow||view!=='people-preview')return;
 const data=event.data;
 const addingAlbum=data?.type==='familyfoto-add-person-album';
 const viewingGroup=data?.type==='familyfoto-open-person-gallery';
 if((!addingAlbum&&!viewingGroup&&data?.type!=='familyfoto-open-person-photo')||!Number.isSafeInteger(data.groupId)||data.groupId<=0||(!addingAlbum&&!viewingGroup&&(!Number.isSafeInteger(data.mediaId)||data.mediaId<=0)))return;
 const ticket=++peoplePhotoRequest;
 const reply=value=>event.source.postMessage({type:'familyfoto-person-photo-status',requestId:data.requestId,...value},location.origin);
 reply({opened:false});
 if(viewingGroup){openPeoplePhotos(data.groupId);reply({opened:true});return;}
 try{
  if(addingAlbum){
   if(data.mediaIds!==undefined&&(!Array.isArray(data.mediaIds)||data.mediaIds.some(id=>!Number.isSafeInteger(id)||id<=0)))throw Error('Invalid photo selection');
   const selected=data.mediaIds===undefined?null:[...new Set(data.mediaIds)];
   albums.add(selected||[],{refreshAfter:false,onSaved:()=>reply({albumSaved:true}),resolveIds:async()=>{
    const result=await api(`/api/people-preview/groups/${data.groupId}/photos`);
    const available=new Set(result.items.map(m=>m.id));
    if(selected?.some(id=>!available.has(id)))throw Error('Selected photos changed; reopen the group and review the selection');
    const ids=selected||[...available];
    if(!ids.length)throw Error('This group has no available photos');
    return ids;
   }});reply({opened:true});return;
  }
  const result=await api(`/api/people-preview/groups/${data.groupId}/photos`);
  if(ticket!==peoplePhotoRequest||view!=='people-preview')return;
  if(!result.items.some(m=>m.id===data.mediaId))throw Error('This photo is no longer available in the group');
  openViewer(data.mediaId,{items:result.items,page:1,pages:1,filters:{kind:'image'}});
  if(!$('viewer').open)throw Error('Photo viewer did not open. Reload the entire FamilyFoto browser tab.');
  reply({opened:true});
 }catch(error){notice(error.message);reply({error:error.message});}
});
async function loadStorage(){
 faceService.load();
 $('storageRefresh').disabled=true;
 await storageSettings.load();
 await uploadSettings.load();
 await backupSettings.load();
 try{const [d,ignored]=await Promise.all([api('/api/storage'),api('/api/import/ignore-patterns')]);$('ignorePatterns').textContent=[...ignored.patterns,...(ignored.no_extension?['No extension']:[])].join(', ');
  for(const role of ['source','destination']){const disk=d[role];fields(role+'Details',{'Path':disk.root,'Serial':disk.serial,'Volume serial':disk.volume_serial,...(role==='source'?{'Access':'Read only'}:{}),...(disk.error?{'Capacity unavailable':disk.error}:{'Capacity':size(disk.total),'Used':`${size(disk.used)} (${(disk.used/disk.total*100).toFixed(1)}%)`,'Free':size(disk.free)})});}
  $('storageChecked').textContent='Checked '+new Date(d.checked_at).toLocaleString();
  fields('monitorDetails',{'State':latest?.monitor?.state||'Not running','Last check':latest?.monitor?.updated,'Reason':latest?.monitor?.reason||'None'});
 }catch(e){notice(e.message);}finally{$('storageRefresh').disabled=false;}
}
async function showView(){
 view=viewForRole(view,document.body.dataset.role);
 pageLoading.reset();
 albums.hide();items=[];updateGallery($('grid'),[]);
 previewPanel.hidden=view!=='people-preview';
 trashSelection.sync([],view);
 ++request;remember();const b=document.querySelector(`nav [data-view="${view==='import-overview'?'imports':view}"]`);
 document.querySelectorAll('nav [data-view]').forEach(n=>n.classList.toggle('active',n===b));$('heading').textContent=view==='import-overview'?'Import overview':b.textContent;document.title=`${$('heading').textContent} - FamilyFoto`;
 $('library').hidden=!(['all','image','video','trash'].includes(view)||(view==='albums'&&albumId!==null));$('imports').hidden=view!=='imports';$('uploads').hidden=view!=='uploads';$('storage').hidden=view!=='settings';
 updateViewLayout();
 if(view==='people-preview')renderPeopleHeading();
 if(view==='people-preview'){if(!peopleResultsReady)finishPeopleLoading=pageLoading.start('Loading people...');if(!previewPanel.getAttribute('src'))previewPanel.src='/people-preview?desktop=1&jobs=1&integrated=1';$('summary').textContent='Run Discover people from each import on the Imports page.';}
 else if(view==='albums'){try{if(albumId!==null){await albums.detail(albumId,albumCollection);await load();}else if(collectionId!==null){await albums.collectionDetail(collectionId);}else{await albums.index();$('summary').textContent='';}}catch(e){notice(e.message);}}
 else if(view==='imports'||view==='uploads')loadImportHistory(true);
 else if(view==='import-overview'){ $('summary').textContent='';loadOverview();}
 else if(view==='settings')await loadStorage();else await load();
}
document.querySelectorAll('nav [data-view]').forEach(b=>b.onclick=()=>{
 view=b.dataset.view;albumId=null;collectionId=null;albumCollection=null;page=1;
 if(view==='people-preview')openPeopleHome();
 showView();
});
async function openCollection(id){collectionId=id;albumId=null;albumCollection=null;view='albums';await showView();}
async function openAlbum(id,{collection=null}={}){
 collectionId=null;albumCollection=collection;
 albumId=id;view='albums';page=1;clearTimeout(debounce);$('search').value='';$('year').value='all';$('importFilter').value='all';await showView();
}
function viewLibrary(id){
 albumId=null;
 const state=importNavigation(id);view=state.view;page=state.page;clearTimeout(debounce);
 $('search').value=state.search;$('year').value=state.year;
 if(![...$('importFilter').options].some(o=>o.value===state.import_job))$('importFilter').add(new Option('Import #'+id,state.import_job));
 $('importFilter').value=state.import_job;showView();
}
$('importFilter').onchange=()=>{page=1;load();};
$('storageRefresh').onclick=loadStorage;
const storageSettings=setupStorageSettings({api,refresh:loadStorage});
const uploadSettings=setupUploadSettings({api});
const backupSettings=setupBackupSettings({api});
const faceService=setupFaceService({api});
window.addEventListener('pagehide',remember);
$('previous').onclick=()=>{page--;load();};$('next').onclick=()=>{page++;load();};$('year').onchange=()=>{page=1;load();};let debounce;$('search').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>{page=1;load();},250);};
$('pageNumber').onchange=()=>{const value=Number($('pageNumber').value);if(!Number.isInteger(value)||value<1){$('pageNumber').value=page;return;}page=Math.min(value,Number($('pageNumber').max)||1);load();};
$('pageNumber').onkeydown=event=>{if(event.key==='Enter'){$('pageNumber').onchange();$('pageNumber').blur();}};
async function poll(){if(backupActive())return;try{void refreshPeopleStatus();const value=await api('/api/status');if(backupActive())return;renderStatus(value);if(view==='imports'||view==='uploads')loadImportHistory();if((latest.job?.state==='running'||latest.video?.state==='running')&&!$('viewer').open)load(true);}catch(e){if(backupActive())return;latest=null;for(const id of ['runDot','sideDot']){$(id).className='dot idle';$(id).title='Cannot reach FamilyFoto. Processing may still be running on the server.';}$('runTime').textContent='Connection lost';$('sideState').textContent='Connection lost';notice(e.message);}}
const manualImport=setupManualImport({api,notice,refresh:poll});
const uploadsView=setupUploadsView({api,notice,refresh:poll});
historyContainer.addEventListener('pipeline-status',event=>manualImport.setActivity(event.detail));
lucide.createIcons();
api('/api/version').then(build=>{const label=$('buildLabel');label.textContent=buildLabel(build);label.title=buildTitle(build);label.hidden=!label.textContent;}).catch(()=>{});
api('/api/me').then(async user=>{document.body.dataset.role=user.role;document.body.dataset.user=user.username;setupAccountSecurity({api,user,notice});setupUserManagement({api,user,notice});previewNav.hidden=!user.capabilities?.people_preview;if(view==='uploads'&&user.role==='guest')view='all';if(view==='people-preview'&&!user.capabilities?.people_preview)view='all';if(['people','faces'].includes(view))view=previewNav.hidden?'all':'people-preview';if(user.role==='admin'){try{const settings=await api('/api/storage/settings');manualImport.setDefault(settings.latest_source||settings.source);manualImport.setStorage(settings);}catch(e){notice(e.message);}}showView();poll();setInterval(poll,5000);}).catch(e=>notice(e.message));
