import {confirmAction} from './confirm-dialog.mjs';
import {createPeopleActionProgress} from '../face_detect/web/people-review-progress.mjs';
import {setupPeopleOrder} from '../face_detect/web/people-order.mjs';
import {editCover} from './album-cover.mjs';

export function setupAlbumCollections({api,element,button,card,modal,openCollection,openAlbum,openPerson}){
 let busy=false;
 modal.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
 const write=(path,method,data)=>api('/api/album-collections'+path,{method,headers:{'Content-Type':'application/json'},...(data?{body:JSON.stringify(data)}:{})});
 const orderKey=item=>`${item.pilot||''}:${item.id}`;
 async function chooseCover(collection){
  modal.replaceChildren();modal.setAttribute('aria-label','Choose collection cover');
  const title=element('h2','Choose cover'),list=element('div',null,'collection-cover-choices'),status=element('p');status.setAttribute('role','status');
  const close=button('Cancel',()=>modal.close()),actions=element('div',null,'album-form-actions');actions.append(close);modal.append(title,list,status,actions);modal.showModal();
  const progress=createPeopleActionProgress(status);
  function choice(source,kind){
   const mid=kind==='person'?source.cover_media_id:source.cover_id;if(!mid)return;
   const tile=card({id:source.id,name:source.name,cover_id:mid,item_count:kind==='person'?source.photo_count:source.item_count},async()=>{
    if(busy)return;busy=true;list.inert=true;close.disabled=true;progress.start('Saving collection cover...');
    try{await write(`/${collection.id}/cover`,'POST',{kind,source_id:kind==='person'?source.person_id:source.id,media_id:mid,...(kind==='person'?{pilot:source.pilot}:{})});modal.close();await openCollection(collection.id);}
    catch(error){progress.fail(error);}finally{busy=false;list.inert=false;close.disabled=false;}
   });tile.title=`${kind==='person'?'Person':'Album'}: ${source.name}`;list.append(tile);
  }
  for(const album of collection.albums)choice(album,'album');
  if(collection.person_count&&document.body.dataset.role==='admin'){
   status.textContent='Loading people covers...';
   try{const data=await api(`/api/album-collections/${collection.id}/people`);if(!list.isConnected||!modal.open)return;for(const person of data.people)if(person.available)choice(person,'person');status.textContent='';}
   catch(error){status.textContent=error.message;}
  }
  if(!list.children.length&&!status.textContent)status.textContent='Add an album with photos or a person to choose a cover.';
 }
 function bindOrder(list,kind,cid,items,progress,enabled=()=>true){
  setupPeopleOrder(list,async(index,targetIndex,after)=>{
   const rows=items(),source=rows[index],target=rows[targetIndex];if(!source||!target)return;
   progress.start('Saving order...');list.inert=true;
   try{
    const result=await write(cid===null?'/order/root':`/${cid}/order`,'POST',{kind,id:source.id,target_id:target.id,after,...(kind==='person'?{pilot:source.pilot,target_pilot:target.pilot}:{})});
    if(!list.isConnected)return;
    const nodes=new Map([...list.querySelectorAll('[data-order-index]')].map(tile=>[orderKey(rows[Number(tile.dataset.orderIndex)]),tile.closest('.collection-child')||tile]));
    for(const item of result.order)if(nodes.has(orderKey(item)))list.append(nodes.get(orderKey(item)));
    progress.finish();
   }catch(error){progress.fail(error);}finally{list.inert=false;}
  },{selector:'.album-card[data-order-index]',idKey:'orderIndex',scrollElement:document.getElementById('albums'),enabled:()=>document.body.dataset.role!=='guest'&&enabled()});
 }
 function edit(collection=null,parentId=null,management=[]){
  const form=element('form'),title=element('h2',collection?'Edit collection':'Create collection');
  form.id='albumForm';
  const name=element('input');name.required=true;name.maxLength=200;name.value=collection?.name||'';
  const description=element('textarea');description.maxLength=4000;description.value=collection?.description||'';
  const nameLabel=element('label','Name'),descriptionLabel=element('label','Description (optional)');
  nameLabel.append(name);descriptionLabel.append(description);
  const visibility=element('input');visibility.type='checkbox';visibility.id='collectionVisibility';visibility.checked=collection?.show_on_albums!==false;visibility.disabled=!(collection?.parent_id??parentId);
  const visibilityLabel=element('label',null,'album-visibility');visibilityLabel.append(visibility,document.createTextNode('Show on Albums page'));visibilityLabel.hidden=!collection;
  visibilityLabel.title=visibility.disabled?'Add this collection to a parent collection before hiding it from Albums':'';
  const status=element('p'),actions=element('div',null,'album-form-actions');
  const cancel=button('Cancel',()=>modal.close()),save=element('button',collection?'Save':'Create','album-primary');save.type='submit';
  actions.append(cancel,save);form.append(title,nameLabel,descriptionLabel,visibilityLabel,status,actions);modal.replaceChildren(form);
  const managementTools=element('div',null,'collection-edit-actions');
  for(const control of management){
   if(control.hidden)continue;
   const action=button(control.textContent,async()=>{
    if(busy)return;
    const changed=name.value!==collection.name||description.value!==(collection.description||'')||visibility.checked!==(collection.show_on_albums!==false);
    if(changed&&!await confirmAction('Discard unsaved collection details?',{title:'Unsaved changes',confirmLabel:'Discard changes'}))return;
    modal.close();control.click();
   },true);managementTools.append(action);
  }
  if(managementTools.children.length)title.after(managementTools);
  const progress=createPeopleActionProgress(status);
  form.onsubmit=async event=>{
   event.preventDefault();if(busy)return;busy=true;managementTools.inert=true;for(const control of [name,description,cancel,save,visibility])control.disabled=true;
   progress.start('Saving collection...');
   try{const result=await write(collection?`/${collection.id}`:'',collection?'PATCH':'POST',{name:name.value,description:description.value,...(!collection?{parent_id:parentId}:{show_on_albums:visibility.checked})});modal.close();await openCollection(result.id);}
   catch(error){progress.fail(error);}
   finally{busy=false;managementTools.inert=false;for(const control of [name,description,cancel,save])control.disabled=false;visibility.disabled=!(collection?.parent_id??parentId);}
  };
  modal.showModal();name.focus();
 }
 function personCard(person,action){
  const tile=card({id:person.person_id,name:person.name||'Person unavailable',item_count:person.photo_count||0},action);
  tile.removeAttribute('data-album');tile.dataset.person=person.person_id;tile.classList.add('collection-person-card');
  const frame=tile.querySelector('.album-cover'),placeholder=frame.querySelector('.album-cover-placeholder');
  placeholder.textContent='';placeholder.innerHTML='<i data-lucide="user-round" aria-hidden="true"></i>';
  if(person.cover_detection_id){const image=element('img');image.alt='';image.loading='lazy';image.src=`/api/people-preview/thumbnails/${person.cover_detection_id}`;image.onerror=()=>{image.hidden=true;};frame.insertBefore(image,frame.querySelector('.album-count-overlay'));}
  const badge=element('span',null,'collection-badge');badge.innerHTML='<i data-lucide="user-round" aria-hidden="true"></i>';badge.title='Person (live photos)';frame.append(badge);
  tile.setAttribute('aria-label',`${person.name||'Person unavailable'}, ${person.photo_count||0} photos`);
  return tile;
 }
 async function picker(collection,people=false){
  const title=element('h2',people?'Add people':'Add albums'),status=element('p'),grid=element('div',null,'album-list collection-picker');
  const actions=element('div',null,'album-form-actions'),selected=new Set(),included=new Set(people?[]:collection.albums.map(album=>album.id));
  let pilot;
  const cancel=button('Cancel',()=>modal.close()),add=button('Add',submit,true);add.disabled=true;
  actions.append(cancel,add);modal.replaceChildren(title,status,grid,actions);modal.showModal();
  const progress=createPeopleActionProgress(status);progress.start(people?'Loading people...':'Loading albums...');
  try{
   const data=await api(people?`/api/album-collections/${collection.id}/person-options`:'/api/albums');if(!grid.isConnected||!modal.open)return;
   pilot=data.pilot;
   if(people)for(const id of data.included||[])included.add(id);
   const items=people?data.people.map(p=>({...p,id:p.person_id})):data.albums;
   for(const album of items){
    const tile=(people?personCard:card)(album,()=>{if(selected.has(album.id))selected.delete(album.id);else selected.add(album.id);tile.classList.toggle('collection-selected',selected.has(album.id));tile.setAttribute('aria-pressed',String(selected.has(album.id)));add.disabled=!selected.size;});
    tile.setAttribute('aria-pressed','false');tile.disabled=included.has(album.id);
    if(tile.disabled){tile.title='Already in this collection';tile.setAttribute('aria-label',album.name+', already included');}
    grid.append(tile);
   }
   globalThis.lucide?.createIcons();progress.finish(items.length?'':people?'No named people available. Run Discover people and name a person first.':'No albums available.');
  }catch(error){progress.fail(error);}
  async function submit(){
   if(busy||!selected.size)return;busy=true;grid.inert=true;add.disabled=cancel.disabled=true;progress.start(people?'Adding people...':'Adding albums...');
   try{await write(`/${collection.id}/${people?'people':'albums'}`,'POST',people?{pilot,person_ids:[...selected]}:{album_ids:[...selected]});modal.close();await openCollection(collection.id);}
   catch(error){progress.fail(error);}
   finally{busy=false;grid.inert=false;cancel.disabled=false;add.disabled=!selected.size;}
  }
 }
 async function hierarchyPicker(collection,adding){
  const title=element('h2',adding?'Add collection':'Move collection'),select=element('select'),label=element('label',adding?'Collection':'Destination');label.append(select);
  const status=element('p'),actions=element('div',null,'album-form-actions');
  const cancel=button('Cancel',()=>modal.close()),save=button(adding?'Add':'Move',submit,true);save.disabled=true;
  actions.append(cancel,save);modal.replaceChildren(title,label,status,actions);modal.showModal();
  const progress=createPeopleActionProgress(status);progress.start('Loading collections...');let choices=[];
  try{
   const data=await api(`/api/album-collections/${collection.id}/${adding?'child':'parent'}-options`);
   if(!select.isConnected||!modal.open)return;choices=data.collections;
   select.append(new Option(adding?'New collection':'Albums (top level)',adding?'new':''));
   for(const item of choices)select.append(new Option(item.path.join(' / '),String(item.id)));
   if(!adding)select.value=collection.parent_id===null?'':String(collection.parent_id);
   save.disabled=false;progress.finish();
  }catch(error){progress.fail(error);}
  async function submit(){
   if(busy)return;
   if(adding&&select.value==='new'){modal.close();edit(null,collection.id);return;}
   const child=adding?choices.find(item=>item.id===Number(select.value)):collection;
   if(adding&&child?.parent_id!=null&&!await confirmAction(`Move "${child.name}" into "${collection.name}"? It will leave its current parent collection.`,{title:'Move collection?',confirmLabel:'Move'}))return;
   busy=true;save.disabled=cancel.disabled=select.disabled=true;progress.start('Moving collection...');
   try{await write(`/${child.id}/parent`,'POST',{parent_id:adding?collection.id:(select.value?Number(select.value):null)});modal.close();await openCollection(collection.id);}
   catch(error){progress.fail(error);}
   finally{busy=false;save.disabled=cancel.disabled=select.disabled=false;}
  }
 }
 function renderIndex(container,collections,onRemove=null,{parentId=null,progress=null,enabled=()=>true}={}){
  if(!collections.length)return;
  const list=element('div',null,'album-list collection-list');list.setAttribute('aria-label','Album collections');
  if(!progress){const status=element('p');container.append(status);progress=createPeopleActionProgress(status);}
  for(const collection of collections){
   const total=collection.album_count+(collection.collection_count||0)+(collection.person_count||0);
   const tile=card({...collection,item_count:total},()=>openCollection(collection.id));
   tile.classList.add('album-collection-card');tile.removeAttribute('data-album');tile.dataset.collection=collection.id;
   tile.dataset.orderIndex=collections.indexOf(collection);
   tile.setAttribute('aria-label',`${collection.name}, ${collection.collection_count||0} collections, ${collection.album_count} albums, ${collection.person_count||0} people`);
   tile.querySelector('.album-count-overlay').setAttribute('aria-label',`${total} direct items`);
   const placeholder=tile.querySelector('.album-cover-placeholder>span');if(placeholder&&!collection.cover_id)placeholder.textContent=total?'Collection':'Empty collection';
   const icon=element('span',null,'collection-badge');icon.innerHTML='<i data-lucide="folders" aria-hidden="true"></i>';icon.title='Album collection';tile.querySelector('.album-cover').append(icon);
   if(onRemove){
    const wrapper=element('div',null,'collection-child');wrapper.append(tile);
    const remove=button('',()=>onRemove(collection,remove),true);remove.classList.add('collection-detach');remove.title='Remove from collection';remove.setAttribute('aria-label',`Remove ${collection.name} from collection`);remove.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';wrapper.append(remove);list.append(wrapper);
   }else list.append(tile);
  }
  container.append(list);globalThis.lucide?.createIcons();
  bindOrder(list,'collection',parentId,()=>collections,progress,enabled);
 }
 function renderDetail(container,collection,headerActions){
  const crumbs=element('nav',null,'collection-breadcrumbs');crumbs.setAttribute('aria-label','Collection path');crumbs.append(button('Albums',()=>openCollection(null)));
  for(const ancestor of collection.breadcrumbs||[{id:collection.id,name:collection.name}]){crumbs.append(element('span','/'));const link=button(ancestor.name,()=>openCollection(ancestor.id));link.dataset.id=ancestor.id;if(ancestor.id===collection.id){link.disabled=true;link.setAttribute('aria-current','page');}crumbs.append(link);}
  const summary=element('div',null,'album-detail-summary collection-detail-summary');
  const copy=element('div',null,'album-detail-copy');
  const title=element('h2',collection.name),description=element('p',collection.description||'','album-description');description.title=collection.description||'';description.hidden=!collection.description;
  const counts=element('p',`${collection.collections?.length||0} collections, ${collection.albums.length} albums, ${collection.person_count||0} people`,'muted collection-counts');
  copy.append(title,description,counts);
  const preview=card({...collection,item_count:0},()=>{}).querySelector('.album-cover');preview.querySelector('.album-count-overlay')?.remove();
  const headerButtons=element('div',null,'album-detail-actions collection-header-actions');summary.append(preview,copy,headerButtons);container.append(crumbs,summary);
  const tools=element('div',null,'collection-tools'),status=element('p'),list=element('div',null,'album-list');
  const selected=new Set();let selecting=false,working=false;
  const select=button('Select',()=>{selecting=!selecting;selected.clear();paint();});
  const all=button('Select all',()=>{for(const album of collection.albums)selected.add(album.id);paint();});
  const remove=button('Remove albums',async()=>{
   if(working||!selected.size)return;working=true;paint();progress.start('Removing albums...');
   try{await write(`/${collection.id}/remove`,'POST',{album_ids:[...selected]});await openCollection(collection.id);}
   catch(error){progress.fail(error);}
   finally{working=false;paint();}
  },true);
  const useCover=button('Use as cover',async()=>{
   const album=collection.albums.find(a=>selected.has(a.id));if(working||selected.size!==1||!album?.cover_id)return;
   working=true;paint();progress.start('Saving collection cover...');
   try{await write(`/${collection.id}/cover`,'POST',{kind:'album',source_id:album.id,media_id:album.cover_id});selected.clear();await openCollection(collection.id);}
   catch(error){progress.fail(error);}finally{working=false;paint();}
  },true);
  useCover.title='Use the selected album cover for this collection';
  const editButton=button('Edit collection',()=>edit(collection,null,[choose,adjust,add,addPeople,addCollection,moveCollection,select,del]),true),add=button('Add albums',()=>picker(collection),true);
  const addPeople=button('Add people',()=>picker(collection,true),true);addPeople.hidden=document.body.dataset.role!=='admin';
  const del=button('Delete collection',async()=>{
   if(!await confirmAction(`Delete collection "${collection.name}"? All albums, people and photos will remain. Child collections will return to Albums.`,{title:'Delete collection?',confirmLabel:'Delete collection',danger:true}))return;
   working=true;paint();progress.start('Deleting collection...');
   try{await write(`/${collection.id}`,'DELETE');await openCollection(null);}catch(error){progress.fail(error);}finally{working=false;paint();}
  },true);
  const back=button('Back to Albums',()=>openCollection(null));
  const addCollection=button('Add collection',()=>hierarchyPicker(collection,true),true);addCollection.hidden=(collection.depth||1)>=4;
  const moveCollection=button('Move collection',()=>hierarchyPicker(collection,false),true);
  const choose=button('Choose cover',()=>chooseCover(collection),true);
  const adjust=button('Adjust cover',()=>editCover({album:collection,api,endpoint:`/api/album-collections/${collection.id}/cover-framing`,onSaved:()=>openCollection(collection.id)}),true);adjust.hidden=!collection.cover_id;
  headerButtons.append(editButton,back);
  tools.append(select,all,remove,useCover);container.append(tools,status);const progress=createPeopleActionProgress(status);
  function editable(node,field,label){
   if(document.body.dataset.role==='guest')return;
   const show=()=>{node.replaceChildren();node.hidden=false;const trigger=button(collection[field]||'Add description',start,true);trigger.className='collection-inline-label album-write';trigger.title=label;trigger.setAttribute('aria-label',label);node.append(trigger);};
   function start(){
    if(working)return;
    const input=element('input');input.value=collection[field]||'';input.maxLength=field==='name'?200:4000;input.setAttribute('aria-label',label);
    const save=button('',async()=>{
     if(working)return;if(field==='name'&&!input.value.trim()){input.setCustomValidity('Enter a collection name');input.reportValidity();return;}
     working=true;paint();input.disabled=save.disabled=cancel.disabled=true;progress.start('Saving collection...');
     try{await write(`/${collection.id}`,'PATCH',{name:field==='name'?input.value:collection.name,description:field==='description'?input.value:collection.description});collection[field]=input.value.trim();if(field==='name'){title.title=collection.name;crumbs.querySelector('[aria-current]').textContent=collection.name;document.title=collection.name+' - FamilyFoto';const heading=document.getElementById('heading');if(heading)heading.textContent=collection.name;}show();progress.finish();}
     catch(error){progress.fail(error);}finally{working=false;paint();input.disabled=save.disabled=cancel.disabled=false;}
    },true),cancel=button('',show);
    for(const [control,icon,text] of [[save,'check','Save'],[cancel,'x','Cancel']]){control.innerHTML=`<i data-lucide="${icon}" aria-hidden="true"></i>`;control.title=text;control.setAttribute('aria-label',text);}
    node.replaceChildren(input,save,cancel);node.classList.add('collection-inline-editor');input.oninput=()=>input.setCustomValidity('');input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();save.click();}if(e.key==='Escape'&&!working){e.preventDefault();show();}};globalThis.lucide?.createIcons();input.focus();input.select();
   }
   show();
  }
  editable(title,'name','Edit collection name');editable(description,'description','Edit collection description');
  renderIndex(container,collection.collections||[],async(child,control)=>{
   control.disabled=true;progress.start('Removing collection...');
   try{await write(`/${child.id}/parent`,'POST',{parent_id:null});await openCollection(collection.id);}catch(error){progress.fail(error);}finally{control.disabled=false;}
  },{parentId:collection.id,progress,enabled:()=>!working});container.append(list);
  for(const album of collection.albums){
   const tile=card(album,()=>{
    if(!selecting){openAlbum(album.id,{collection});return;}
    if(selected.has(album.id))selected.delete(album.id);else selected.add(album.id);paint();
   });tile.dataset.orderIndex=collection.albums.indexOf(album);
   const wrapper=element('div',null,'collection-child'),removeAlbum=button('',async()=>{
    if(working)return;working=true;paint();progress.start('Removing album from collection...');
    try{await write(`/${collection.id}/remove`,'POST',{album_ids:[album.id]});await openCollection(collection.id);}
    catch(error){progress.fail(error);}finally{working=false;paint();}
   },true);
   removeAlbum.classList.add('collection-detach');removeAlbum.title='Remove from collection';removeAlbum.setAttribute('aria-label',`Remove ${album.name} from collection`);removeAlbum.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';
   wrapper.append(tile,removeAlbum);list.append(wrapper);
  }
  bindOrder(list,'album',collection.id,()=>collection.albums,progress,()=>!selecting&&!working);
  globalThis.lucide?.createIcons();
  if(!collection.albums.length&&!(collection.collections||[]).length&&!collection.person_count)list.append(element('p','No albums or collections here yet.','muted collection-empty'));
  if(document.body.dataset.role==='admin'&&collection.person_count){
   const peopleList=element('div',null,'album-list collection-people'),peopleStatus=element('p');container.append(peopleStatus,peopleList);
   const peopleProgress=createPeopleActionProgress(peopleStatus);
   let personOrder=[],loadingPeople=false;
   bindOrder(peopleList,'person',collection.id,()=>personOrder,peopleProgress,()=>!loadingPeople&&!working);
   const loadPeople=async()=>{
    if(loadingPeople||peopleList.getAttribute('aria-busy')==='true')return;loadingPeople=true;
    peopleProgress.start('Loading people...');
    try{
     const data=await api(`/api/album-collections/${collection.id}/people`);if(!peopleList.isConnected)return;peopleList.replaceChildren();personOrder=data.people.map(p=>({id:p.person_id,pilot:p.pilot}));
     for(const person of data.people){
      const wrapper=element('div',null,'collection-child');
      const tile=personCard(person,()=>openPerson?.(collection.id,person));tile.disabled=!person.available;
      tile.dataset.orderIndex=data.people.indexOf(person);
      const removePerson=button('',async()=>{
       removePerson.disabled=true;peopleProgress.start('Removing person from collection...');
       try{await write(`/${collection.id}/remove-people`,'POST',{pilot:person.pilot,person_ids:[person.person_id]});await openCollection(collection.id);}
       catch(error){peopleProgress.fail(error);}finally{removePerson.disabled=false;}
      },true);
      removePerson.classList.add('collection-detach');removePerson.title='Remove from collection';removePerson.setAttribute('aria-label',`Remove ${person.name||'unavailable person'} from collection`);removePerson.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';
      wrapper.append(tile,removePerson);peopleList.append(wrapper);
     }
     peopleProgress.finish(data.warning||'');globalThis.lucide?.createIcons();
    }catch(error){peopleProgress.fail(error);}finally{loadingPeople=false;}
   };
   loadPeople();
  }
  function paint(){
   select.textContent=selecting?'Done selecting':'Select';all.hidden=remove.hidden=useCover.hidden=!selecting;
   tools.hidden=!selecting;
   select.title='Select albums to remove from this collection or use as its cover';
   for(const control of [select,all,...headerButtons.querySelectorAll('button')])control.disabled=working;
   useCover.disabled=working||selected.size!==1||!collection.albums.find(a=>selected.has(a.id))?.cover_id;
   remove.disabled=working||!selected.size;list.inert=working;
   for(const tile of list.querySelectorAll('[data-album]')){const active=selected.has(Number(tile.dataset.album));tile.classList.toggle('collection-selected',active);if(selecting)tile.setAttribute('aria-pressed',String(active));else tile.removeAttribute('aria-pressed');}
  }
  paint();
 }
 return {edit,renderIndex,renderDetail};
}
