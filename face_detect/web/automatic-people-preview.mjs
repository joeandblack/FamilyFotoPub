// Standalone preview only. No import from the live application entry point.
import '../../web/session-activity.mjs';
import {peopleSection} from './people-sections.mjs';
import {setupReviewSelection} from './people-review-selection.mjs';
import {setupPeopleOrder} from './people-order.mjs';
import {setupPeopleSuggestions} from './people-suggestions.mjs';
import {setupAutoOrganize} from './people-auto-organize.mjs';
import {reviewSnapshot} from './people-review-snapshot.mjs';
import {setupPhotoSelection} from './people-photo-selection.mjs';
import {makeDialogMovable} from '../../web/movable-dialog.mjs';
import {setupGroupPages,groupPage,syncGroupCards} from './people-group-pages.mjs';
import {pageSizeControl,reviewImportFromURL} from '../../web/page-size.mjs';
import {personChoices} from './people-choices.mjs';
import {bindPersonMenu} from '../../web/person-menu.mjs';
import {peopleWarningLabel,peopleWarningDetail} from './people-scan-warnings.mjs';
import {runPeopleReview,createPeopleActionProgress} from './people-review-progress.mjs';
const integrated=new URLSearchParams(location.search).has('integrated');
const endpoint=integrated?'/api/people-preview':'/api/automatic-people';let state;
let refreshIgnored=async()=>{},refreshBlurry=async()=>{},suggestionsPanel;
let reviewImport=reviewImportFromURL(location.search),autoOrganizePanel;
const activeImport=()=>location.hash==='#review-faces'?reviewImport:null;
const scopedBody=body=>activeImport()?{...body,import_id:activeImport()}:body;
if(integrated){document.querySelector('.notice').hidden=true;document.querySelector('header').hidden=true;document.title='People';}
const desktopMode=new URLSearchParams(location.search).has('desktop');
const element=(tag,text,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
const reviewSection=element('section');reviewSection.id='people-review-section';
const reviewHeading=element('h2','To be reviewed');reviewHeading.id='people-review-heading';
const reviewGroups=element('div');reviewGroups.id='review-groups';reviewGroups.setAttribute('aria-labelledby',reviewHeading.id);
reviewSection.append(reviewHeading,reviewGroups);document.querySelector('#groups').after(reviewSection);
async function reviewAction(section,action,ids,personId,progress){
 const result=await runPeopleReview(endpoint,scopedBody({section,action,revision:state.revision,group_ids:ids,person_id:personId,...reviewSnapshot(state.groups,ids,true)}),progress);
 if(result.updates){applyReviewUpdates(result.updates);await loadReviewPages();}
 else {progress({phase:'refreshing'});await refresh();}
 return result;
}
function applyReviewUpdates(updates){
 const changed=new Map(updates.groups.map(group=>[group.id,group]));
 const removed=new Set(updates.removed_group_ids);
 state.groups=state.groups.filter(group=>!removed.has(group.id)).map(group=>{
  const next=changed.get(group.id)||group;changed.delete(group.id);return next;
 });
 state.groups.push(...changed.values());state.revision=updates.revision;state.people=updates.people;
 syncGroupCards(document.querySelector('#groups'),state.groups.filter(g=>peopleSection(g)==='people'),g=>card(g));
 reviewPages.update(state.groups.filter(g=>peopleSection(g)==='review'));
 otherPages.update(state.groups.filter(g=>peopleSection(g)==='other'));
 renderMatchedPeople();globalThis.lucide?.createIcons();
}
const reviewSelection=setupReviewSelection(reviewGroups,reviewGroups,(ids,progress)=>reviewAction('review','set-aside',ids,null,progress),{
 people:()=>personChoices(state),match:(ids,personId,progress)=>reviewAction('review','match',ids,personId,progress),
 ignore:(ids,progress)=>reviewAction('review','ignore',ids,null,progress)
});
const otherGroups=document.querySelector('#outliers');
const otherSelection=setupReviewSelection(otherGroups,otherGroups,null,{
 label:'Unnamed groups in Review later',people:()=>personChoices(state),
 match:(ids,personId,progress)=>reviewAction('other','match',ids,personId,progress),
 ignore:(ids,progress)=>reviewAction('other','ignore',ids,null,progress)
});
const reviewPages=setupGroupPages(reviewGroups,{render:group=>card(group),selection:reviewSelection,label:'To be reviewed',load:()=>loadReviewPages('review')});
const otherPages=setupGroupPages(otherGroups,{render:group=>card(group),selection:otherSelection,label:'Review later',load:()=>loadReviewPages('other')});
const reviewPageTickets={review:0,other:0};
async function loadReviewPages(only=null){
 if(location.hash!=='#review-faces')return;
 await Promise.all(['review','other'].filter(section=>!only||section===only).map(async section=>{
  const pages=section==='review'?reviewPages:otherPages,grid=section==='review'?reviewGroups:otherGroups;
  const ticket=++reviewPageTickets[section],scope=activeImport(),generation=refreshTicket;
  pages.busy(true);
  grid.setAttribute('aria-busy','true');
  try{
   const data=await request('/review-page?'+new URLSearchParams({section,...pages.parameters()}));
   if(ticket!==reviewPageTickets[section]||generation!==refreshTicket||scope!==activeImport()||location.hash!=='#review-faces')return;
   state.groups=[...state.groups.filter(g=>peopleSection(g)!==section),...data.groups];
   state.revision=data.revision;state.people=data.people;
   pages.updatePage(data);
   if(section==='review'){reviewHeading.textContent=`To be reviewed (${data.total})`;reviewSection.hidden=!data.total;}
   else document.querySelector('#outlier-summary').textContent=`Review later (${data.total}) · optional`;
  }catch(error){if(ticket===reviewPageTickets[section])document.querySelector('#message').textContent=error.message;}
  finally{if(ticket===reviewPageTickets[section]){grid.setAttribute('aria-busy','false');pages.busy(false);}}
 }));
}
let completedScan=null,recheckBusy=false;
setupPeopleOrder(document.querySelector('#groups'),async(id,targetId,after)=>{
 pageAction.start('Saving people order...');
 try{await request(`/groups/${id}/position`,{revision:state.revision,order_revision:state.order_revision,target_id:targetId,after});await refresh();pageAction.finish();}
 catch(error){await refresh();pageAction.fail(error);}
});
const pageAction=createPeopleActionProgress(document.querySelector('#message'));
let photoClick=0,photoTimer,albumCompletion;
function openPersonPhoto(groupId,mediaId,album=false,mediaIds,gallery=false,onAlbumSaved=null){
 const requestId=++photoClick;const message=document.querySelector('#message');
 if(album)albumCompletion={requestId,onSaved:onAlbumSaved};
 if(album)pageAction.finish();else pageAction.start('Opening photo...');clearTimeout(photoTimer);
 parent.postMessage({type:gallery?'familyfoto-open-person-gallery':album?'familyfoto-add-person-album':'familyfoto-open-person-photo',groupId,mediaId,mediaIds,requestId},location.origin);
 if(!album)photoTimer=setTimeout(()=>{if(requestId===photoClick)pageAction.fail('Reload the entire FamilyFoto browser tab to activate the photo viewer (not just Refresh inside People).');},5000);
}
window.addEventListener('message',event=>{
 if(event.origin!==location.origin||event.source!==parent||event.data?.type!=='familyfoto-person-photo-status')return;
 if(event.data.albumSaved&&albumCompletion?.requestId===event.data.requestId){albumCompletion.onSaved?.();albumCompletion=null;return;}
 if(event.data.requestId!==photoClick)return;
 if(albumCompletion?.requestId===event.data.requestId){if(event.data.error)pageAction.fail(event.data.error);return;}
 clearTimeout(photoTimer);if(event.data.error)pageAction.fail(event.data.error);else if(event.data.opened)pageAction.finish();else pageAction.start('Opening photo...');
});
const recheck=element('button','Detect again');recheck.id='recheck-unnamed';recheck.disabled=true;recheck.title='Available after Discover people finishes';
const recheckStatus=element('span',undefined,'muted');recheckStatus.setAttribute('role','status');
const reviewRecheck=element('button','Detect again');reviewRecheck.id='recheck-review';reviewRecheck.type='button';
const reviewRecheckStatus=element('span',undefined,'muted');reviewRecheckStatus.setAttribute('role','status');
if(desktopMode)reviewSelection.toolbar.append(reviewRecheckStatus);
reviewRecheck.onclick=()=>recheck.onclick();
function recheckMessage(text){for(const indicator of recheckIndicators)indicator.finish(text);}
if(desktopMode)otherSelection.toolbar.append(recheckStatus);
const recheckIndicators=[recheckStatus,reviewRecheckStatus].map(status=>createPeopleActionProgress(status,{inline:true}));
const recheckReason=element('span',undefined,'muted');recheckReason.id='recheck-reason';
if(integrated)recheck.after(recheckReason);
async function updateRecheckEligibility(){
 completedScan='completed-imports';
 recheck.disabled=reviewRecheck.disabled=recheckBusy;recheckReason.textContent='';
 recheck.title=reviewRecheck.title=recheckBusy?'Comparing faces...':'Compare unnamed faces in To be reviewed and Review later with named people using saved results';
}
if(integrated)updateRecheckEligibility();
recheck.onclick=async()=>{
 if(!completedScan||recheckBusy)return;
 recheckBusy=true;recheck.disabled=reviewRecheck.disabled=true;
 const showProgress=text=>{for(const indicator of recheckIndicators)indicator.start(text);};
 showProgress('Preparing Detect again...');
 try{
  if(!state)await refresh();if(!state)throw Error('People results could not be loaded. Try Refresh.');
  const result=await runPeopleReview(endpoint,scopedBody({revision:state.revision}),event=>{
   showProgress(event.phase==='waiting'?'Waiting for the current operation to finish its step...':event.phase==='comparing'?'Comparing faces...':'Preparing Detect again...');
  },{path:`/jobs/${completedScan}/recheck/stream`});
  showProgress('Updating results...');await refresh();
  recheckMessage(` ${result.matched} matched, ${result.remaining} still unnamed, ${result.suggestions||0} possible matches to review`);
  if(result.suggestions)await suggestionsPanel?.open();
  if(reviewSection.hidden)document.querySelector('#message').textContent=recheckStatus.textContent;
 }
 catch(error){for(const indicator of recheckIndicators)indicator.fail(error);}
 finally{recheckBusy=false;if(integrated)await updateRecheckEligibility();else recheck.disabled=reviewRecheck.disabled=!completedScan;}
};
async function request(path,body){
 if(body?.group_ids&&/^\/(review|other)\/(match|ignore|set-aside)$/.test(path))body={...body,...reviewSnapshot(state?.groups||[],body.group_ids,true)};
 if(activeImport()){
  if(body)body=scopedBody(body);
  else if(path===''||/^\/(cards|review-page|groups\/\d+|suggestions|auto-organize)(\?|$)/.test(path))path+=(path.includes('?')?'&':'?')+'import_id='+activeImport();
 }
 const response=await fetch(endpoint+path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{cache:'no-store'});const value=await response.json();if(!response.ok){const error=Error(value.detail||'Unable to update People');error.status=response.status;throw error;}return value;
}
let facePreviewSerial=0;
function attachFacePreview(trigger,face,actions=[],onPhoto){
 const panel=element('div',undefined,'face-actions-panel');panel.setAttribute('popover','auto');panel.setAttribute('role','dialog');panel.setAttribute('aria-label','Photo preview');
 panel.id=`face-preview-${++facePreviewSerial}`;trigger.setAttribute('aria-controls',panel.id);trigger.setAttribute('aria-expanded','false');
 const bar=element('div',undefined,'face-preview-toolbar');
 const iconButton=(icon,label,cls)=>{const b=element('button',undefined,cls);b.type='button';b.title=label;b.setAttribute('aria-label',label);b.innerHTML=`<i data-lucide="${icon}" aria-hidden="true"></i>`;return b;};
 const more=iconButton('ellipsis','Photo actions','face-menu-toggle');more.hidden=!actions.length;more.setAttribute('aria-haspopup','menu');more.setAttribute('aria-expanded','false');
 const close=iconButton('x','Close photo preview','face-actions-close');close.onclick=()=>panel.hidePopover();bar.append(more,close);
 const menu=element('div',undefined,'face-context-menu');menu.hidden=true;menu.id=panel.id+'-menu';more.setAttribute('aria-controls',menu.id);menu.setAttribute('role','menu');menu.setAttribute('aria-label','Photo actions');
 const status=element('p',undefined,'face-action-status');status.setAttribute('role','status');
 panel.append(bar,menu,status);trigger.after(panel);let preview,busy=false;
 const indicator=createPeopleActionProgress(status);
 function place(){const r=trigger.getBoundingClientRect(),p=panel.getBoundingClientRect();panel.style.left=Math.max(8,Math.min(r.left,innerWidth-p.width-8))+'px';panel.style.top=Math.max(8,Math.min(r.bottom+5,innerHeight-p.height-8))+'px';}
 function renderMenu(items=actions,back=false){
  menu.replaceChildren();
  if(back){const previous=element('button','Back');previous.type='button';previous.setAttribute('role','menuitem');previous.onclick=()=>{renderMenu();menu.querySelector('button')?.focus();};menu.append(previous);}
  for(const action of items){
   const button=element('button',action.label,action.className);button.type='button';button.setAttribute('role','menuitem');
   if(action.children)button.setAttribute('aria-haspopup','menu');
   button.onclick=async()=>{
    if(busy)return;
    if(action.children){renderMenu(action.children,true);menu.querySelector('button')?.focus();return;}
    busy=true;indicator.start(action.progressLabel||`${action.label}...`);menu.hidden=true;place();
    for(const b of panel.querySelectorAll('button'))b.disabled=true;
    try{await action.run();indicator.finish();if(panel.isConnected)panel.hidePopover();}
    catch(error){indicator.fail(error);menu.hidden=false;place();}
    finally{busy=false;for(const b of panel.querySelectorAll('button'))b.disabled=false;}
   };menu.append(button);
  }
  if(panel.matches(':popover-open'))place();
 }
 function showMenu(){if(!actions.length||busy)return;renderMenu();menu.hidden=false;more.setAttribute('aria-expanded','true');place();menu.querySelector('button')?.focus();}
 more.onclick=()=>{if(menu.hidden)showMenu();else{menu.hidden=true;more.setAttribute('aria-expanded','false');place();}};
 menu.onkeydown=event=>{
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();menu.hidden=true;more.setAttribute('aria-expanded','false');more.focus();place();return;}
  if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;
  event.preventDefault();const buttons=[...menu.querySelectorAll('button:not(:disabled)')],index=buttons.indexOf(document.activeElement);
  buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowUp'?-1:1)+buttons.length)%buttons.length]?.focus();
 };
 function show(){
  if(integrated&&face.media_id&&!preview){
   preview=element(onPhoto?'button':'div',undefined,'face-original-preview');
   if(onPhoto){preview.type='button';preview.title='View full photo';preview.setAttribute('aria-label','View full photo');preview.onclick=()=>{panel.hidePopover();onPhoto();};}
   const image=element('img');image.alt=face.name||'Original photo thumbnail';image.decoding='async';image.src=`/api/media/${face.media_id}/thumbnail`;
   image.onerror=()=>{image.hidden=true;status.textContent='Photo preview unavailable.';place();};preview.append(image);bar.after(preview);
  }
  if(!panel.matches(':popover-open'))panel.showPopover();place();globalThis.lucide?.createIcons();
 }
 trigger.onclick=()=>{if(panel.matches(':popover-open'))panel.hidePopover();else show();};
 const context=event=>{if(trigger.closest('.selecting-photos'))return;event.preventDefault();show();showMenu();};
 trigger.oncontextmenu=context;panel.oncontextmenu=context;
 trigger.addEventListener('keydown',event=>{if(event.key==='ContextMenu'||(event.shiftKey&&event.key==='F10'))context(event);});
 panel.addEventListener('toggle',()=>{const opened=panel.matches(':popover-open');trigger.setAttribute('aria-expanded',String(opened));if(!opened){menu.hidden=true;more.setAttribute('aria-expanded','false');}});
 return panel;
}
function card(group,full=false,existingImages=new Map()){
 const box=element('article',undefined,'card');box.dataset.group=group.id;
 const photoCount=group.photo_count??(new Set(group.faces.map(f=>f.media_id).filter(Boolean)).size||group.faces.length);
 let heading;
 if(full||group.name){
  heading=element('h2',group.name||'Unnamed group');const count=element('span',`${photoCount} ${photoCount===1?'photo':'photos'}`,'muted');
  if(full){const header=element('div',undefined,'group-detail-heading');header.append(heading,count);box.append(header);}else box.append(heading,count);
 }
 const photos=element('div',undefined,'photos');
 const selectedPhotos=new Set();
 if(desktopMode)photos.style.flexWrap='wrap';
 const pageData=full?groupPage(group.faces,detailPage,detailPageSize):null;
 if(full)detailPage=pageData.page;
 (full?pageData.items:[group.faces.find(f=>f.detection_id===group.cover_detection_id)||group.faces[0]].filter(Boolean)).forEach((face,index)=>{
  const wrapper=element('div');wrapper.dataset.face=face.detection_id;
  const source=`${endpoint}/thumbnails/${face.detection_id}`;
  const existing=existingImages.get(String(face.detection_id));
  const reused=desktopMode&&existing?.getAttribute('src')===source;
  const tile=reused?existing:desktopMode?element('img',undefined,'photo'):element('span','◯','photo');
  if(desktopMode&&!reused){tile.loading='lazy';tile.decoding='async';tile.width=58;tile.height=64;tile.src=source;tile.alt='Detected face';tile.style.objectFit='cover';}else if(!desktopMode)tile.setAttribute('aria-label',`Photo ${index+1} placeholder`);
  if(!full){
   const open=element('button',undefined,'group-photo');open.type='button';open.title='View group';open.setAttribute('aria-label',`View ${group.name||'unnamed group'}`);
   open.append(tile);open.onclick=()=>showDetail(group.id);wrapper.append(open);
   if(group.name){const caption=element('span',undefined,'person-name-overlay');caption.append(element('span',group.name,'person-name'),element('span',String(photoCount),'person-count'));open.append(caption);}
   else{open.append(element('span',String(photoCount),'unnamed-count-overlay'));open.setAttribute('aria-label',`View unnamed group, ${photoCount} ${photoCount===1?'photo':'photos'}`);}
  }else if(desktopMode){
   const open=element('button',undefined,'face-actions-trigger');open.type='button';open.title='Photo actions';open.setAttribute('aria-label','Photo actions');open.setAttribute('aria-expanded','false');
   open.append(tile);wrapper.append(open);
  }else wrapper.append(tile);
  if(full&&integrated&&face.media_id){wrapper.dataset.id=face.detection_id;wrapper.setAttribute('aria-label',`Face ${index+1}`);}
  if(desktopMode&&full){
   const actions=[];
   if(group.name&&face.detection_id!==group.cover_detection_id)actions.push({label:'Set as cover',className:'choose-cover',run:async()=>{
    await request(`/groups/${group.id}/cover`,{revision:state.revision,detection_id:face.detection_id,expected_cover_id:group.cover_detection_id});detailUndoReady=false;await refreshAffectedGroups(group.id);
   }});
   const targets=personChoices(state).filter(g=>g.person_id!==group.person_id);
   if(targets.length)actions.push({label:'Move to person',className:'move-face',children:targets.map(target=>({label:target.name,progressLabel:`Moving face to ${target.name}...`,run:async()=>{
    await request(`/faces/${face.detection_id}/correct`,{revision:state.revision,person_id:target.person_id,...reviewSnapshot([group],[group.id])});detailUndoReady=true;await refreshAffectedGroups(group.id,target.person_id);
   }}))});
   actions.push({label:'Ignore',className:'ignore-face',run:async()=>{
    await request(`/faces/${face.detection_id}/ignored`,{revision:state.revision,enabled:true,...reviewSnapshot([group],[group.id])});detailUndoReady=false;await refreshAffectedGroups(group.id);
   }});
   attachFacePreview(wrapper.querySelector('.face-actions-trigger'),face,actions,()=>openPersonPhoto(group.id,face.media_id));
  }photos.append(wrapper);
 });box.append(photos);
 if(full){
  const displayName=group.name||'Unnamed group';
  const rename=element('button',displayName);rename.type='button';rename.className='editable-person-name';rename.title='Edit person name';rename.setAttribute('aria-label',`Edit name: ${displayName}`);heading.replaceChildren(rename);
  const form=element('form');form.hidden=true;form.className='inline-person-name';
  const input=element('input');input.name='rename';input.value=group.name||'';input.required=true;input.maxLength=100;input.setAttribute('aria-label','Person name');
  const save=element('button');save.title='Save name';save.setAttribute('aria-label','Save name');save.innerHTML='<i data-lucide="check" aria-hidden="true"></i>';save.type='submit';
  const cancel=element('button');cancel.title='Cancel';cancel.setAttribute('aria-label','Cancel name edit');cancel.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';cancel.type='button';
  const error=element('p');error.setAttribute('role','alert');let renameProgress;
  rename.onclick=()=>{input.value=group.name||'';error.textContent='';form.hidden=false;heading.hidden=true;input.focus();input.select();};
  cancel.onclick=()=>{form.hidden=true;heading.hidden=false;rename.focus();error.textContent='';};
  input.onkeydown=event=>{if(event.key==='Escape'&&!save.disabled){event.preventDefault();cancel.click();}};
  form.onsubmit=async event=>{event.preventDefault();if(!input.value.trim()){input.focus();return;}save.disabled=cancel.disabled=input.disabled=true;renameProgress.start('Saving name...');
   try{
    if(group.name)await request(`/people/${group.person_id}/rename`,{name:input.value,revision:state.revision,expected_name:group.name});
    else await request(`/groups/${group.id}/name`,{name:input.value,revision:state.revision,...reviewSnapshot([group],[group.id])});
    await refresh();renameProgress.finish();
   }
   catch(e){renameProgress.fail(e);}finally{save.disabled=cancel.disabled=input.disabled=false;}
  };
  form.append(input,save,cancel);heading.after(form);heading.parentElement.after(error);
  renameProgress=createPeopleActionProgress(error);
 }
 let albumButton,albumCount,bulkMove,bulkIgnore,bulkLater,bulkBusy=false;
 if(full&&pageData.total){
  const nav=element('nav',undefined,'people-group-pages group-face-pages');nav.setAttribute('aria-label','Group photos pages');
  const previous=element('button'),next=element('button'),input=element('input'),total=element('span',`/ ${pageData.pages}`),range=element('span',`${pageData.start+1}-${pageData.start+pageData.items.length} / ${pageData.total} faces`);
  for(const [button,icon,label] of [[previous,'chevron-left','Previous page'],[next,'chevron-right','Next page']]){button.type='button';button.title=label;button.setAttribute('aria-label',label);button.innerHTML=`<i data-lucide="${icon}" aria-hidden="true"></i>`;}
  input.type='number';input.min=1;input.max=pageData.pages;input.value=pageData.page;input.setAttribute('aria-label','Group photos page');
  previous.disabled=pageData.page===1;next.disabled=pageData.page===pageData.pages;
  for(const control of [previous,input,total,next])control.hidden=pageData.pages===1;
  const go=value=>{if(bulkBusy){input.value=detailPage;return;}detailPage=groupPage(group.faces,value,detailPageSize).page;showDetail(group.id,true,detailView);};
  previous.onclick=()=>go(detailPage-1);next.onclick=()=>go(detailPage+1);input.onchange=()=>go(input.value);
  const size=pageSizeControl(detailPageSize,value=>{if(bulkBusy){size.querySelector('select').value=String(detailPageSize);return;}detailPageSize=value;detailPage=1;showDetail(group.id,true,detailView);},'Group photos items per page');
  nav.append(previous,input,total,next,range,size);photos.before(nav);
 }
 function syncAlbum(){if(albumButton)albumButton.disabled=bulkBusy||!selectedPhotos.size;if(bulkMove)bulkMove.disabled=bulkBusy||!selectedPhotos.size||!personChoices(state).some(p=>p.person_id!==group.person_id);if(bulkIgnore)bulkIgnore.disabled=bulkBusy||!selectedPhotos.size;if(bulkLater)bulkLater.disabled=bulkBusy||!selectedPhotos.size;if(albumCount){albumCount.hidden=!selectedPhotos.size;albumCount.textContent=selectedPhotos.size?`${selectedPhotos.size} selected`:'';}}
 if(integrated&&full){
  const tools=element('div',undefined,'group-album-tools');
  const photoSelection=setupPhotoSelection(photos,tools,value=>{selectedPhotos.clear();for(const id of value)selectedPhotos.add(id);syncAlbum();});
  for(const button of tools.querySelectorAll('button'))if(button.textContent==='Select all')button.title='Select all faces on this page';
  albumButton=element('button','Add to album');albumButton.type='button';albumButton.className='add-group-album';
  albumButton.onclick=()=>{if(selectedPhotos.size)openPersonPhoto(group.id,null,true,[...new Set(group.faces.filter(f=>selectedPhotos.has(f.detection_id)).map(f=>f.media_id))],false,()=>photoSelection.clear());};
  const albumAll=element('button','Add all to album');albumAll.type='button';albumAll.className='add-all-group-album';albumAll.title='Add all photos in this group across every page';
  albumAll.onclick=()=>openPersonPhoto(group.id,null,true,activeImport()?[...new Set(group.faces.map(f=>f.media_id))]:undefined,false,()=>photoSelection.clear());
  bulkMove=element('button','Move to person');bulkMove.type='button';bulkMove.className='bulk-move-faces';bulkMove.setAttribute('aria-expanded','false');
  bulkIgnore=element('button','Ignore');bulkIgnore.type='button';bulkIgnore.className='bulk-ignore-faces';
  const personMenu=bindPersonMenu(bulkMove,()=>personChoices(state).filter(p=>p.person_id!==group.person_id),id=>perform('move',id));
  const status=element('p',undefined,'face-action-status');status.setAttribute('role','status');
  let indicator;
  async function perform(action,personId){
   if(bulkBusy||!selectedPhotos.size)return;
   const ids=[...selectedPhotos];bulkBusy=true;indicator.start(`${action==='ignore'?'Ignoring':'Moving'} ${ids.length} selected faces...`);
   personMenu.close();photos.inert=true;for(const b of tools.querySelectorAll('button'))b.disabled=true;
   try{const result=await request(`/groups/${group.id}/selected-faces`,{revision:state.revision,...reviewSnapshot([group],[group.id]),detection_ids:ids,action,person_id:personId??null});selectedPhotos.clear();detailUndoReady=action==='move';await refreshAffectedGroups(group.id,personId,result.group_ids||[]);indicator.finish();}
   catch(e){indicator.fail(e);}
   finally{bulkBusy=false;photos.inert=false;for(const b of tools.querySelectorAll('button'))b.disabled=false;syncAlbum();}
  }
  bulkIgnore.onclick=()=>perform('ignore');
  albumCount=element('span',undefined,'group-selection-count muted');albumCount.setAttribute('role','status');box.querySelector('.group-detail-heading').append(albumCount);tools.append(albumButton,albumAll,bulkMove,bulkIgnore);photos.before(tools,status);syncAlbum();
  indicator=createPeopleActionProgress(status);
  if(peopleSection(group)!=='other'){
   bulkLater=element('button','Move to Review later');bulkLater.type='button';bulkLater.className='group-review-later';bulkLater.title='Move selected faces to Review later';tools.append(bulkLater);
   bulkLater.onclick=()=>perform('review-later');syncAlbum();
  }
 }
 if(!full){
  box.classList.add('person-card');
  if(group.name)box.classList.add('named-person-card');
  else box.classList.add('unnamed-person-card');
  const footer=element('div',undefined,'card-footer');
  for(const child of [...box.children]){
   if(child===photos||child.tagName==='H2'||child.classList.contains('muted'))continue;
   footer.append(child);
  }
  if(footer.children.length)box.append(footer);
 }
 return box;
}
let detailGroupId,detailTicket=0,detailPage=1,detailPageSize=100,detailView,detailUndoReady=false;
function updateGroupState(view){
 const group=view.group;state.revision=view.revision;
 const index=state.groups.findIndex(g=>g.id===group.id);
 const previousSection=index>=0?peopleSection(state.groups[index]):null;
 const membersChanged=index<0||state.groups[index].faces.map(f=>f.detection_id).join(',')!==group.faces.map(f=>f.detection_id).join(',');
 if(index>=0){if(group.faces.length)state.groups[index]=group;else state.groups.splice(index,1);}else if(group.faces.length)state.groups.push(group);
 const tile=document.querySelector(`.person-card[data-group="${group.id}"]`);
 if(tile){if(group.faces.length)tile.replaceWith(card(group));else tile.remove();}
 else if(group.name&&group.faces.length)document.querySelector('#groups').append(card(group));
 if(previousSection!==peopleSection(group)||(membersChanged&&peopleSection(group)!=='people')){
  void loadReviewPages();
 }
 renderMatchedPeople();
}
async function refreshAffectedGroups(gid,personId,extraIds=[]){
 const target=state.groups.find(g=>g.person_id===personId);
 const ids=new Set([...extraIds,...(target?[target.id]:[])]);ids.delete(gid);
 await Promise.all([
  showDetail(gid,true),
  ...[...ids].map(async id=>updateGroupState(await request(`/groups/${id}`)))
 ]);
 globalThis.lucide?.createIcons();
}
async function showDetail(gid,quiet=false,providedView=null){
 if(!quiet){detailPage=1;detailView=null;detailUndoReady=false;}
 const ticket=++detailTicket;detailGroupId=gid;
 let dialog=document.querySelector('#group-detail');
 if(!dialog){
  dialog=element('dialog');dialog.id='group-detail';document.body.append(dialog);dialog.addEventListener('close',()=>{if(!dialog.open)detailTicket++;});
  const bar=element('div',undefined,'group-detail-bar');bar.tabIndex=0;bar.setAttribute('aria-label','Move group window');bar.title='Drag to move window';
  const close=element('button');close.title='Close';close.setAttribute('aria-label','Close group photos');close.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';close.addEventListener('click',()=>{detailTicket++;dialog.close();});bar.append(element('span'),close);
  bar.firstElementChild.className='group-detail-title';dialog.append(bar);makeDialogMovable(dialog,bar);
 }
 const bar=dialog.querySelector('.group-detail-bar');
 if(!quiet){
  const cachedGroup=state?.groups.find(g=>g.id===gid),count=cachedGroup?.face_count??cachedGroup?.faces.length??0;
  dialog.style.width=`min(${Math.max(740,Math.min(1100,48+Math.min(count,14)*76))}px,94vw)`;
  dialog.style.height='fit-content';dialog.style.margin='auto';dialog.style.left='';dialog.style.top='';
 }
 const loading=element('span','Loading group...','people-detail-loading group-detail-status');loading.setAttribute('role','status');
 bar.querySelector('.group-detail-status')?.remove();bar.querySelector('.group-detail-title').append(loading);
 const cached=state?.groups.find(group=>group.id===gid);
 if(quiet&&dialog.open){
  // Keep the existing photos visible until the replacement group is ready.
 }else if(cached&&!cached.summary_only){
  const preview=card(cached,true);preview.setAttribute('aria-busy','true');
  for(const child of preview.children){
   if(child.classList.contains('group-detail-heading')){for(const item of child.children)item.inert=true;}
   else child.inert=true;
  }
  preview.querySelector('.group-detail-heading').append(bar);
  loading.textContent='Updating group...';
  dialog.replaceChildren(preview);
 }else dialog.replaceChildren(bar);
 if(!dialog.open)dialog.showModal();
 try{
  const view=providedView||await request(`/groups/${gid}`);if(ticket!==detailTicket||!dialog.open)return;
  detailView=view;
  updateGroupState(view);
  // Preserve loaded images from the cached preview and quiet updates before creating new ones.
  const existingImages=new Map([...dialog.querySelectorAll('[data-face] img.photo')].map(image=>[image.closest('[data-face]').dataset.face,image]));
  const content=card(view.group,true,existingImages);
  let tools=content.querySelector('.group-album-tools');
  if(!tools){tools=element('div',undefined,'group-album-tools');content.prepend(tools);}
  const undoable=detailUndoReady&&view.history.find(item=>item.can_undo);
  const undo=element('button','Undo');undo.type='button';undo.className='undo-correction';undo.hidden=undo.disabled=!undoable;undo.title=undoable?`Undo ${undoable.action} for face ${undoable.detection_id}`:'No correction available to undo';tools.append(undo);
  if(undoable){
   const error=element('p',undefined,'face-action-error');error.setAttribute('role','status');tools.after(error);
   const indicator=createPeopleActionProgress(error);
   undo.addEventListener('click',async()=>{undo.disabled=true;indicator.start('Undoing correction...');try{const result=await request(`/events/${undoable.id}/undo`,{revision:view.revision});detailUndoReady=false;await refreshAffectedGroups(gid,null,result.group_ids||[]);indicator.finish();}catch(e){indicator.fail(e);undo.disabled=false;}});
  }
  loading.textContent='';loading.hidden=true;
  content.querySelector('.group-detail-heading').append(bar);
  const scroll=dialog.scrollTop;dialog.replaceChildren(content);if(quiet)dialog.scrollTop=scroll;globalThis.lucide?.createIcons();if(!dialog.open)dialog.showModal();
 }catch(e){if(ticket===detailTicket&&dialog.open){loading.textContent=e.message;loading.className='face-action-error group-detail-status';}}
}
let refreshTicket=0,peopleLoadState={loading:true};
function reportPeopleLoading(){if(integrated)parent.postMessage({type:'familyfoto-people-loading',...peopleLoadState,scope:activeImport()},location.origin);}
async function refresh(){const ticket=++refreshTicket;peopleLoadState={loading:true};reportPeopleLoading();document.querySelector('#groups').setAttribute('aria-busy','true');
 try{const result=await request('/cards');if(ticket!==refreshTicket)return;state=result;syncGroupCards(document.querySelector('#groups'),state.groups.filter(g=>peopleSection(g)==='people'),g=>card(g));document.querySelector('#message').textContent='';if(location.hash==='#review-faces')await loadReviewPages();else {reviewPages.update([]);otherPages.update([]);}globalThis.lucide?.createIcons();if(document.querySelector('#group-detail')?.open)await showDetail(detailGroupId);peopleLoadState={loading:false};}
 catch(error){if(ticket!==refreshTicket)return;peopleLoadState={loading:false,error:error.message};document.querySelector('#message').textContent=error.message;}
 finally{if(ticket===refreshTicket){renderMatchedPeople();reviewSelection.refresh();otherSelection.refresh();document.querySelector('#groups').setAttribute('aria-busy','false');reportPeopleLoading();}}
 if(ticket===refreshTicket&&!peopleLoadState.error&&!recheckBusy)recheckMessage('');
 if(ticket===refreshTicket&&!peopleLoadState.error&&location.hash==='#ignored-faces')await refreshIgnored();
 if(ticket===refreshTicket&&!peopleLoadState.error&&location.hash==='#review-faces')suggestionsPanel?.refresh();
 if(integrated&&ticket===refreshTicket&&location.hash==='#review-faces')await updateRecheckEligibility();
}
if(desktopMode)suggestionsPanel=setupPeopleSuggestions({request,endpoint,viewGroup:showDetail,before:reviewSection,recheck});
if(integrated)autoOrganizePanel=setupAutoOrganize({endpoint,request,after:recheck,refresh,scope:activeImport});
const reviewPage=element('section');reviewPage.id='review-faces-page';reviewPage.hidden=true;
const importFilter=element('select');importFilter.id='review-import-filter';importFilter.setAttribute('aria-label','Filter review faces by import');
importFilter.style.cssText='width:auto;max-width:100%;min-width:190px';
const allImports=element('option','All imports');allImports.value='';importFilter.append(allImports);
const filterRow=element('div');filterRow.className='review-import-filter';filterRow.style.cssText='display:flex;align-items:center;gap:12px;margin:0 0 16px';
const filterLabel=element('label','Import');filterLabel.htmlFor=importFilter.id;filterRow.append(filterLabel,importFilter);reviewPage.append(filterRow);
const filterStatus=element('span',undefined,'muted');filterStatus.setAttribute('role','status');filterStatus.style.fontSize='12px';filterRow.append(filterStatus);
const matchedSection=element('section');matchedSection.id='review-matched-people';matchedSection.hidden=true;
const matchedHeading=element('h2','Matched people'),matchedGrid=element('div');matchedGrid.id='review-matched-groups';
matchedHeading.style.cssText='font-size:15px;font-weight:600;margin:16px 0 8px';
const matchedEmpty=element('p',undefined,'muted');matchedEmpty.style.fontSize='12px';
matchedSection.append(matchedHeading,matchedGrid,matchedEmpty);reviewPage.append(matchedSection);
function renderMatchedPeople(){
 const matched=activeImport()?(state?.groups||[]).filter(g=>peopleSection(g)==='people'):[];
 matchedSection.hidden=!activeImport();syncGroupCards(matchedGrid,matched,g=>card(g));
 matchedHeading.textContent=`Matched people (${matched.length})`;
 matchedEmpty.textContent=state?.groups?.some(g=>peopleSection(g)!=='people')?'':matched.length?'All current review faces in this import are already matched.':'No faces available for review in this import.';
 matchedEmpty.hidden=!matchedEmpty.textContent;
}
async function loadReviewImports(){
 if(!integrated)return;
 try{
  const choices=await request('/review-imports');importFilter.replaceChildren(allImports);
  for(const choice of choices.filter(c=>c.id.startsWith('import:'))){const option=element('option',choice.label);option.value=choice.id.split(':')[1];importFilter.append(option);}
  if(reviewImport&&!choices.some(c=>c.id==='import:'+reviewImport)){const option=element('option',`Import #${reviewImport}`);option.value=String(reviewImport);importFilter.append(option);}
  importFilter.value=reviewImport||'';importFilter.title='';
 }catch(error){importFilter.title=error.message;}
}
if(location.hash==='#review-faces')loadReviewImports();
importFilter.onchange=async()=>{
 if(reviewSelection.isBusy()||otherSelection.isBusy()||recheckBusy||autoOrganizePanel?.isBusy()){importFilter.value=reviewImport||'';return;}
 reviewImport=Number(importFilter.value)||null;
 const url=new URL(location.href);if(reviewImport)url.searchParams.set('import',reviewImport);else url.searchParams.delete('import');history.replaceState(null,'',url);
 reviewSelection.clear();otherSelection.clear();reviewPages.reset();otherPages.reset();suggestionsPanel?.reset();
 document.querySelector('#group-detail')?.close();reviewPage.inert=true;filterStatus.textContent='Loading faces...';filterStatus.classList.add('people-detail-loading');
 try{await refresh();await autoOrganizePanel?.load();}finally{reviewPage.inert=false;filterStatus.textContent='';filterStatus.classList.remove('people-detail-loading');}
};
const reviewBack=element('button','Back to People');reviewBack.type='button';
if(!integrated)reviewPage.append(reviewBack,element('h2','Review faces'));
for(const section of [document.querySelector('#possible-matches'),reviewSection,otherGroups.parentElement])if(section)reviewPage.append(section);
document.querySelector('main').append(reviewPage);
const reviewOpen=element('button','Review faces');reviewOpen.id='review-faces';reviewOpen.type='button';
document.querySelector('#refresh').after(reviewOpen);
reviewOpen.onclick=()=>{location.hash='review-faces';};
reviewBack.onclick=()=>{
 reviewImport=null;importFilter.value='';reviewSelection.clear();otherSelection.clear();reviewPages.reset();otherPages.reset();suggestionsPanel?.reset();
 const url=new URL(location.href);url.searchParams.delete('import');url.hash='';history.replaceState(null,'',url);
 document.querySelector('#groups').replaceChildren();document.querySelector('#group-detail')?.close();
 routeReview();refresh();
};
function routeReview(){
 const active=location.hash==='#review-faces';reviewPage.hidden=!active;
 document.querySelector('main').classList.toggle('show-review',active);
 renderMatchedPeople();
 if(integrated)parent.postMessage({type:'familyfoto-people-view',review:active},location.origin);
}
window.addEventListener('hashchange',()=>{routeReview();if(location.hash==='#review-faces')loadReviewImports();refresh();});routeReview();
let manualRefreshBusy=false;
async function refreshFromHeader(){
 if(manualRefreshBusy)return;manualRefreshBusy=true;
 const button=document.querySelector('#refresh');button.disabled=true;
 document.querySelector('#message').textContent='Refreshing people and Review later...';
 try{
  if(location.hash==='#blurry-faces'){document.querySelector('#message').textContent='';await refreshBlurry();return;}
  await loadReviewImports();
  await refresh();
  if(!peopleLoadState.error&&!document.querySelector('#message').textContent)document.querySelector('#message').textContent=`Updated ${new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}`;
 }finally{manualRefreshBusy=false;button.disabled=false;if(integrated)parent.postMessage({type:'familyfoto-people-refresh-finished'},location.origin);}
}
document.querySelector('#refresh').addEventListener('click',refreshFromHeader);refresh();

if(desktopMode)for(const kind of ['blurry','ignored']){
 const blurry=kind==='blurry',title=blurry?'Blurry faces':'Ignored faces';
 const open=element('button',title);open.id=`${kind}-faces`;
 document.querySelector('#review-faces').after(open);
 const dialog=element('section');dialog.id=`${kind}-face-review`;dialog.hidden=true;
 const summary=element('h2',title);
 const back=element('button','Back to People');back.type='button';
 const content=element('div');content.className='ignored-face-content';
 dialog.append(back,summary,content);document.querySelector('main').append(dialog);let page=1,busy=false,ticket=0;
 const actionStatus=element('p');summary.after(actionStatus);const restoreProgress=createPeopleActionProgress(actionStatus);
 const main=document.querySelector('main');
 function routeIgnored(){
  const active=location.hash===`#${kind}-faces`;main.classList.toggle(`show-${kind}`,active);dialog.hidden=!active;
  if(active)document.querySelector('#message').textContent='';
  if(active)show();
 }
 back.onclick=()=>{location.hash='';};window.addEventListener('hashchange',routeIgnored);
 async function show(){
  const current=++ticket;
  if(dialog.hidden)return;
  content.setAttribute('aria-busy','true');if(!content.children.length)content.append(element('p',`Loading ${title.toLowerCase()}...`,'people-detail-loading'));
  try{
   const data=await request(`/${kind}?page=${page}`);
   if(current!==ticket)return;
   if(page>data.pages){page=data.pages;return show();}
   page=data.page;summary.textContent=`${title} (${data.total})`;
   const grid=element('div');grid.className='ignored-face-grid';
   for(const face of data.items){
    const card=element('figure');card.style.margin='0';
    const image=element('img');image.src=`${endpoint}/${kind}/${face.detection_id}/thumbnail`;image.alt=blurry?'Blurry face':'Ignored face';image.loading='lazy';image.decoding='async';image.width=58;image.height=64;image.style.cssText='width:58px;height:64px;object-fit:contain';
    if(blurry)image.title=face.quality?.reason==='too-small'?'Face is too small':face.quality?.reason==='too-dark'?'Face is too dark':'Face has very little sharp detail';
    const caption=element('figcaption',face.name);caption.title=face.name;
    const restore=element('button');restore.type='button';restore.className='restore-ignored';restore.dataset.face=face.detection_id;
    restore.title=blurry?'Send to review':'Restore face';restore.setAttribute('aria-label',`${restore.title}: ${face.name}`);
    const icon=element('i');icon.dataset.lucide='undo-2';restore.append(icon);
    restore.onclick=async()=>{
     if(busy)return;busy=true;restore.disabled=true;restoreProgress.start(blurry?'Sending face to review...':'Restoring face...');
     try{
      if(blurry)await request(`/blurry/${face.detection_id}/review`,{});
      else await request(`/faces/${face.detection_id}/ignored`,{revision:data.revision,enabled:false});
      card.remove();await show();
      restoreProgress.finish(blurry?'Sent to To be reviewed.':'Face restored.');
      if(blurry)document.querySelector('#message').textContent='Sent to To be reviewed.';
     }
     catch(e){restoreProgress.fail(e);restore.disabled=false;}finally{busy=false;}
    };
    const preview=element('button',undefined,'face-actions-trigger');preview.type='button';preview.title='Photo preview';preview.setAttribute('aria-label',`Preview photo: ${face.name}`);preview.append(image);
    card.append(preview,caption,restore);grid.append(card);
    attachFacePreview(preview,face);
   }
   const footer=element('div');
   for(const [label,step] of [['Previous',-1],['Next',1]]){
    const button=element('button',label);button.disabled=step<0?page===1:page>=data.pages;
    button.onclick=()=>{if(busy)return;page+=step;show();};footer.append(button);
   }
   footer.append(element('span',`${page} / ${data.pages}`));
   content.replaceChildren(grid);if(data.total)content.append(footer);
   globalThis.lucide?.createIcons();
   if(!data.total)grid.append(element('p',`No ${title.toLowerCase()}.`));
  }catch(e){if(current===ticket){content.replaceChildren(element('p',e.message));document.querySelector('#message').textContent=e.message;}}
  finally{if(current===ticket)content.setAttribute('aria-busy','false');}
 }
 open.onclick=()=>{page=1;if(location.hash===`#${kind}-faces`)show();else location.hash=`${kind}-faces`;};
 if(blurry)refreshBlurry=show;else refreshIgnored=show;routeIgnored();
 back.addEventListener('click',()=>refresh());
}

// Production displays global results; import selection and scanning live in Imports.
if(integrated){
 const ready=()=>parent.postMessage({type:'familyfoto-people-ready'},location.origin);
 window.addEventListener('message',event=>{
  if(event.origin!==location.origin||event.source!==parent)return;
  if(event.data?.type==='familyfoto-people-ready-request'){ready();reportPeopleLoading();routeReview();return;}
  if(event.data?.type==='familyfoto-people-action'&&event.data.action==='back-people'){reviewBack.click();return;}
  if(event.data?.type!=='familyfoto-people-action'||!['refresh','review-faces','ignored-faces','blurry-faces'].includes(event.data.action))return;
  if(event.data.action==='refresh'){refreshFromHeader();return;}
  document.getElementById(event.data.action)?.click();
 });
 ready();
}
if(!integrated&&new URLSearchParams(location.search).has('jobs')){
 const controls=element('section',undefined,'card');controls.id='job-controls';
 if(!integrated)controls.append(element('h2','Discover people'));
 const progress=element('p','Ready','muted');progress.id='job-progress';progress.setAttribute('role','status');
 const errors=element('ul');errors.id='job-errors';
 const start=element('button','Start scan','primary');start.id='job-start';
 const pause=element('button','Pause');pause.id='job-pause';
 const resume=element('button','Resume');resume.id='job-resume';
 const cancel=element('button','Cancel');cancel.id='job-cancel';
 const selection=element('select');selection.id='job-selection';selection.setAttribute('aria-label','Import or photo year');selection.hidden=!desktopMode;
 controls.append(progress,selection,start,pause,resume,cancel,errors);
 document.querySelector('header').after(controls);
 const requestedImport=new URLSearchParams(location.search).get('import');
 const fixedReview=!!requestedImport;
 let selectionsReady=false,jobMissing=false,scanTotal=null;
 start.disabled=desktopMode||fixedReview||integrated;selection.disabled=fixedReview;
 if(integrated||fixedReview)sessionStorage.removeItem('automatic-fixture-job');
 function syncSelection(){
  if(integrated||fixedReview){
   if(fixedReview)selection.value='import:'+requestedImport;
   const option=[...selection.options].find(o=>o.value===selection.value);
   if(option&&scanTotal!==null)option.textContent=`Import #${selection.value.split(':')[1]} · ${scanTotal} photos`;
   start.disabled=!(selectionsReady&&jobMissing);
  }
 }
 if(desktopMode){request('/selections').then(rows=>{for(const row of rows.filter(r=>(!integrated||r.id.startsWith('import:'))&&(!fixedReview||r.id==='import:'+requestedImport))){const option=element('option',row.label);option.value=row.id;selection.append(option);}selectionsReady=!!selection.options.length;if(requestedImport)selection.value='import:'+requestedImport;if(integrated||fixedReview){if(!fixedReview)selectJob();syncSelection();}else start.disabled=!selectionsReady;}).catch(e=>document.querySelector('#message').textContent=e.message);}
 let jobId=fixedReview?'import-'+requestedImport:integrated?null:sessionStorage.getItem('automatic-fixture-job'),lastState;
 function selectJob(){jobId=selection.value.startsWith('import:')?'import-'+selection.value.split(':')[1]:null;jobMissing=false;scanTotal=null;lastState=null;completedScan=null;recheck.disabled=true;start.disabled=pause.disabled=resume.disabled=cancel.disabled=true;errors.replaceChildren();progress.textContent='Ready';poll();}
 selection.onchange=selectJob;
 const show=data=>{controls.dataset.job=data.id;controls.dataset.state=data.state;
  const descriptions={queued:'Queued for face detection.',running:'Scanning photos for faces and grouping matching people.','waiting-for-foreground':'Waiting for an import handoff or upload. Browsing does not pause this scan.','awaiting-receipt':'Recording the current photo analysis result before continuing.',paused:'Paused. Resume to continue from the saved progress.',completed:'Scan complete. Discovered people are ready to review.',blocked:'Scan blocked. Review the error before resuming.',cancelled:'Scan cancelled.'};
  const processed=(data.done||0)+(data.excluded||0),percent=data.total?Math.round(processed/data.total*100):0;
  progress.textContent=`${data.id?.startsWith('import-')?'Import #'+data.id.slice(7)+' · ':''}${data.done}/${data.total} ${data.unit||'faces'} analyzed (${percent}% complete)${data.excluded?` · ${data.excluded} excluded`:''}${data.errors?.length?` · ${data.errors.length} warnings`:''}. ${descriptions[data.state]||data.state}${data.current_media_id?` Current photo: #${data.current_media_id}.`:''}`;
  jobMissing=false;scanTotal=data.total;
  completedScan=data.state==='completed'?data.id:null;recheck.disabled=recheckBusy||!completedScan;
  errors.replaceChildren(...(data.errors||[]).map(item=>element('li',`Photo ${item.media_id}: ${peopleWarningLabel(item.error)}. ${peopleWarningDetail(item)}`)));
  const terminal=['completed','cancelled'].includes(data.state);start.disabled=!terminal;
  syncSelection();
  pause.disabled=terminal||data.state==='paused';resume.disabled=terminal||['queued','running','waiting-for-foreground','awaiting-receipt'].includes(data.state);cancel.disabled=terminal;
  if(data.state==='completed'&&lastState!=='completed')refresh();lastState=data.state;};
 async function poll(){if(!jobId)return;const requested=jobId;try{const data=await request(`/jobs/${requested}`);if(jobId===requested)show(data);}catch(error){if(jobId!==requested)return;if(error.status===404){jobMissing=true;syncSelection();}else{start.disabled=true;document.querySelector('#message').textContent=error.message;}}}
 start.addEventListener('click',async()=>{start.disabled=true;try{
  const selected=desktopMode?{selection:selection.value}:await request('/fixture-media');jobId=selection.value.startsWith('import:')?'import-'+selection.value.split(':')[1]:`scan-${Date.now()}`;
  const data=await request('/jobs',{id:jobId,...selected});sessionStorage.setItem('automatic-fixture-job',jobId);show(data);
 }catch(error){if(integrated||fixedReview){jobMissing=false;syncSelection();poll();}else start.disabled=false;document.querySelector('#message').textContent=error.message;}});
 for(const [button,action] of [[pause,'pause'],[resume,'resume'],[cancel,'cancel']])button.addEventListener('click',async()=>{
  if(!jobId)return;button.disabled=true;try{show(await request(`/jobs/${jobId}/control`,{action}));}catch(error){document.querySelector('#message').textContent=error.message;}});
 pause.disabled=resume.disabled=cancel.disabled=true;poll();setInterval(poll,integrated?2000:400);
}
