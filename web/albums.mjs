// Albums contain references only. All names/descriptions are rendered as text.
import {confirmAction} from './confirm-dialog.mjs';
import {createPeopleActionProgress} from '../face_detect/web/people-review-progress.mjs';
import {setupPeopleOrder} from '../face_detect/web/people-order.mjs';
import {setupAlbumCollections} from './album-collections.mjs';
import {applyCover,editCover} from './album-cover.mjs';
const destinationKey='familyfoto.albumDestination';
export function readAlbumDestination(albums,excluded=null,storage){
 try{
  const id=(storage||globalThis.localStorage).getItem(destinationKey);
  return albums.some(album=>String(album.id)===id&&album.id!==excluded)?id:'new';
 }catch{return 'new';}
}
export function rememberAlbumDestination(id,storage){
 if(!Number.isSafeInteger(Number(id))||Number(id)<=0)return;
 try{(storage||globalThis.localStorage).setItem(destinationKey,String(id));}catch{}
}
export function setupAlbums({api,openAlbum,openCollection,openPerson,refresh,notice}){
 const $=id=>document.getElementById(id);
 const section=document.createElement('section');section.id='albums';section.className='details';section.hidden=true;$('library').before(section);
 const header=document.createElement('div');header.id='albumHeader';header.hidden=true;$('library').prepend(header);
 const modal=document.createElement('dialog');modal.id='albumDialog';modal.setAttribute('aria-label','Album');document.body.append(modal);
 const headerActions=document.createElement('div');headerActions.className='albums-header-actions';headerActions.hidden=true;document.querySelector('main > header')?.append(headerActions);
 let current=null,generation=0,saving=false,parentCollection=null;
 const docked=[];
 function restoreHeader(){
  headerActions.hidden=true;headerActions.replaceChildren();
  for(const [node,slot] of docked)slot.replaceWith(node);
  docked.length=0;
  const pageHeader=document.querySelector('main > header');if(pageHeader)pageHeader.hidden=false;
 }
 function dock(node,target){
  if(!node)return;
  const slot=document.createComment('album header position');node.before(slot);docked.push([node,slot]);target.append(node);
 }
 const element=(tag,text,cls)=>{const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;};
 const button=(text,action,write=false)=>{const b=element('button',text,write?'album-write':'');b.type='button';b.onclick=action;return b;};
 const count=n=>`${n} ${Number(n)===1?'item':'items'}`;
 const collections=setupAlbumCollections({api,element,button,card:albumCard,modal,openCollection:openCollection||collectionDetail,openAlbum,openPerson});
 function albumCard(album,action){
  const card=button('',action);card.className='album-card';card.dataset.album=album.id;
  const text=element('span',null,'album-card-copy'),name=element('strong',album.name);name.title=album.name;text.append(name);
  if(album.description){const description=element('span',album.description,'album-card-description');description.title=album.description;text.append(description);}
  const image=cover(album),badge=element('span',String(album.item_count),'album-count-overlay');badge.setAttribute('aria-label',count(album.item_count));image.append(badge);
  card.append(image,text);return card;
 }
 function cover(album){
  const frame=element('span',null,'album-cover');
  const placeholder=element('span',null,'album-cover-placeholder');
  placeholder.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="3" width="16" height="16" rx="2"/><path d="M3 7v13a1 1 0 0 0 1 1h13M5 15l4-4 4 4 3-3 5 5"/><circle cx="15" cy="8" r="1"/></svg>';
  placeholder.append(element('span',album.cover_id?'Preview unavailable':'Ready for memories'));frame.append(placeholder);
  if(album.cover_id){const image=element('img');applyCover(image,album);image.src=`/api/media/${album.cover_id}/thumbnail`;image.alt='';image.loading='lazy';image.onerror=()=>{image.hidden=true;};frame.append(image);}
  return frame;
 }
 async function command(task){try{await task();}catch(e){notice(e.message);}}
 async function index(){
  restoreHeader();
  const ticket=++generation;current=null;header.hidden=true;section.hidden=false;section.textContent='Loading albums...';
  const data=await api('/api/albums');if(ticket!==generation)return;
  const collectionData=await api('/api/album-collections');if(ticket!==generation)return;
  const create=button('Create album',()=>editDialog(),true);create.classList.add('album-primary');headerActions.append(create,button('Create collection',()=>collections.edit(),true));headerActions.hidden=false;section.replaceChildren();
  const surface=element('div');section.append(surface);collections.renderIndex(surface,collectionData.collections||[]);
  const list=element('div',null,'album-list');surface.append(list);
  const status=element('p');list.before(status);const orderProgress=createPeopleActionProgress(status);
  setupPeopleOrder(surface,async(id,targetId,after)=>{
   orderProgress.start('Saving album order...');
   try{
    const result=await api(`/api/albums/${id}/position`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_id:targetId,after})});
    const cards=new Map([...list.querySelectorAll('.album-card')].map(card=>[Number(card.dataset.album),card]));
    for(const aid of result.order)if(cards.has(aid))list.append(cards.get(aid));
    orderProgress.finish();
   }catch(error){orderProgress.fail(error);}
  },{selector:'.album-card[data-album]',idKey:'album',enabled:()=>document.body.dataset.role!=='guest',dropSelector:'.album-collection-card',scrollElement:section,onDrop:async(id,target)=>{
   const cid=Number(target.dataset.collection);orderProgress.start('Adding album to collection...');
   try{
    const result=await api(`/api/album-collections/${cid}/albums`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_ids:[id]})});
    const fresh=await api(`/api/album-collections/${cid}`);
    if(ticket!==generation)return;
    const badge=target.querySelector('.album-count-overlay'),total=fresh.albums.length+(fresh.collections?.length||0)+(fresh.person_count||0);
    badge.textContent=String(total);badge.setAttribute('aria-label',`${total} direct items`);
    target.setAttribute('aria-label',`${fresh.name}, ${fresh.collections?.length||0} collections, ${fresh.albums.length} albums, ${fresh.person_count||0} people`);
    const coverId=fresh.albums.find(album=>album.cover_id)?.cover_id;
    if(coverId&&!target.querySelector('.album-cover>img')){const image=element('img');image.src=`/api/media/${coverId}/thumbnail`;image.alt='';image.loading='lazy';image.onerror=()=>{image.hidden=true;};badge.before(image);}
    orderProgress.finish(result.added===0?'Album already in collection.':'Album added to collection.');
   }catch(error){if(ticket===generation)orderProgress.fail(error);}
  }});
  const visibleAlbums=data.albums.filter(album=>album.show_on_albums!==false);
  if(!data.albums.length){const empty=element('div',null,'albums-empty');empty.append(cover({}),element('h3','A place for every memory'),element('p','Start with an empty album, then add your favorite photos and videos. You can also create one from a Library selection or an import.','muted'),element('small','Your originals always stay in Library.'));list.append(empty);}
  for(const album of visibleAlbums){
   list.append(albumCard(album,()=>openAlbum(album.id)));
  }
 }
 async function collectionDetail(id){
  if(id===null)return index();
  restoreHeader();const ticket=++generation;current=null;header.hidden=true;section.hidden=false;section.textContent='Loading collection...';
  const collection=await api(`/api/album-collections/${id}`);if(ticket!==generation)return;
  $('heading').textContent=collection.name;$('summary').textContent=`${collection.collections?.length||0} collections, ${collection.albums.length} ${collection.albums.length===1?'album':'albums'}, ${collection.person_count||0} people${collection.description?' - '+collection.description:''}`;
  $('summary').title=collection.description;section.replaceChildren();headerActions.hidden=true;collections.renderDetail(section,collection);
  document.title=collection.name+' - FamilyFoto';
  const pageHeader=document.querySelector('main > header');if(pageHeader)pageHeader.hidden=true;
 }
 async function detail(id,collection=parentCollection){
  parentCollection=collection;
  restoreHeader();
  const ticket=++generation;section.hidden=true;header.hidden=false;header.textContent='Loading album...';
  const album=await api(`/api/albums/${id}`);if(ticket!==generation)return;current=album;
  $('heading').textContent=album.name;document.title=album.name+' - FamilyFoto';
  const back=button('All albums',()=>openAlbum(null));back.className='album-back';back.setAttribute('aria-label','Back to all albums');
  const summary=element('div',null,'album-detail-summary');
  const description=element('div',null,'album-detail-copy');
  dock($('heading'),description);
  description.append(element('p',album.description||'A collection of your photos and videos.','album-description'));
  dock($('summary')?.closest('.library-summary-line')||$('summary'),description);
  description.querySelector('.album-description').title=album.description||'A collection of your photos and videos.';
  const actions=element('div',null,'album-detail-actions');
  actions.append(button('Edit album',()=>editDialog({album}),true));
  actions.append(back);
  if(parentCollection){const cid=parentCollection.id;actions.append(button('Back to collection',()=>openCollection(cid)));}
  const tools=element('div',null,'album-detail-tools');dock($('search'),tools);tools.append(actions);
  summary.append(cover(album),description,tools);
  header.replaceChildren(summary);
  const pageHeader=document.querySelector('main > header');if(pageHeader)pageHeader.hidden=true;
 }
 function hide(){restoreHeader();generation++;section.hidden=true;header.hidden=true;current=null;}
 function editDialog({album=null,ids=[],importJob=null,pick=false,onSaved=null,refreshAfter=true,resolveIds=null,moveFrom=null}={}){
  modal.replaceChildren();saving=false;
  const form=element('form');form.id='albumForm';
  const title=element('h2',moveFrom?'Move to album':album?'Edit album':pick?'Add to album':'Create album');
  const target=element('select');target.id='albumTarget';target.append(new Option('New album','new'));
  const targetLabel=element('label','Album');targetLabel.append(target);targetLabel.hidden=!pick;
  const name=element('input');name.id='albumName';name.required=true;name.maxLength=200;name.value=album?.name||'';
  const nameLabel=element('label','Name');nameLabel.append(name);
  const description=element('textarea');description.id='albumDescription';description.maxLength=4000;description.value=album?.description||'';
  const descriptionLabel=element('label','Description (optional)');descriptionLabel.append(description);
  const visibility=element('input');visibility.type='checkbox';visibility.id='albumVisibility';visibility.checked=album?.show_on_albums!==false;visibility.disabled=!album?.collection_count;
  const visibilityLabel=element('label',null,'album-visibility');visibilityLabel.hidden=!album;visibilityLabel.append(visibility,document.createTextNode('Show on Albums page'));
  visibilityLabel.title=visibility.disabled?'Add this album to a collection before hiding it from Albums':'';
  const error=element('p');error.id='albumError';error.setAttribute('role','status');
  const submit=element('button',moveFrom?'Move':album?'Save':pick?'Add':'Create');submit.type='submit';submit.id='albumSave';
  const cancel=button('Cancel',()=>modal.close());
  submit.classList.add('album-primary');const actions=element('div',null,'album-form-actions');actions.append(cancel,submit);
  form.append(title,element('p',album?'Update this collection. Photos and videos stay in Library.':importJob?`Include currently visible media associated with import #${importJob}, including duplicate matches once.`:ids.length?`${ids.length} selected item(s). Originals stay in Library.`:resolveIds?'All photos in this group, across every page. Originals stay in Library.':'Create an empty album and add photos and videos later.','muted'),targetLabel,nameLabel,descriptionLabel,error,actions);modal.append(form);
  descriptionLabel.after(visibilityLabel);
  if(album){
   const management=element('div',null,'album-form-actions');
   if(album.cover_id)management.append(button('Adjust cover',()=>{
    if(saving)return;
    editCover({album,api,onSaved:async()=>{
     await detail(album.id);Object.assign(album,current);
    }});
   },true));
   const remove=button('Delete album',()=>command(async()=>{
    if(saving)return;
    if(!await confirmAction(`Delete album "${album.name}"? Photos and videos will remain in your Library.`,{title:'Delete album?',confirmLabel:'Delete album',danger:true}))return;
    saving=true;remove.disabled=true;
    try{await api(`/api/albums/${album.id}`,{method:'DELETE'});modal.close();await openAlbum(null);}
    finally{saving=false;remove.disabled=false;}
   }),true);
   remove.classList.add('album-delete');management.append(remove);visibilityLabel.after(management);
  }
  const progress=createPeopleActionProgress(error);
  if(moveFrom)form.querySelector('p.muted').textContent=`Move ${ids.length} selected item(s) out of this album. Library and other albums remain unchanged.`;
  target.onchange=()=>{const fresh=target.value==='new';nameLabel.hidden=descriptionLabel.hidden=!fresh;name.required=fresh;};
  if(pick){target.disabled=true;submit.disabled=true;api('/api/albums').then(data=>{if(!form.isConnected)return;for(const a of data.albums)if(a.id!==moveFrom)target.append(new Option(a.name,String(a.id)));target.value=readAlbumDestination(data.albums,moveFrom);target.onchange();target.disabled=false;submit.disabled=false;}).catch(e=>{error.textContent=e.message;});}
  form.onsubmit=async event=>{
   event.preventDefault();if(saving)return;saving=true;submit.disabled=cancel.disabled=target.disabled=name.disabled=description.disabled=visibility.disabled=true;progress.start(moveFrom?'Moving photos to album...':pick?'Adding photos to album...':'Saving album...');
   try{
    const mediaIds=resolveIds?await resolveIds():ids;
    let result;
    if(moveFrom){result=await api(`/api/albums/${moveFrom}/transfer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({media_ids:mediaIds,...(target.value==='new'?{new_album:{name:name.value,description:description.value}}:{target_id:Number(target.value)})})});}
    else if(pick&&target.value!=='new'){await api(`/api/albums/${target.value}/items`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({media_ids:mediaIds})});result={id:Number(target.value)};}
    else result=await api(album?`/api/albums/${album.id}`:'/api/albums',{method:album?'PATCH':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.value,description:description.value,...(!album?{media_ids:mediaIds,import_job:importJob}:{show_on_albums:visibility.checked})})});
    if(pick)rememberAlbumDestination(result.id??target.value);
    progress.finish();modal.close();onSaved?.(result);if(pick){notice(moveFrom?'Moved to album. Originals remain in Library.':'Added to album. Originals remain in Library.');if(moveFrom&&current?.id===moveFrom)await detail(moveFrom);if(refreshAfter)await refresh();}else await openAlbum(result.id,{collection:album?parentCollection:null});
   }catch(e){progress.fail(e);}finally{saving=false;submit.disabled=cancel.disabled=target.disabled=name.disabled=description.disabled=false;visibility.disabled=!album?.collection_count;}
  };
  modal.showModal();(pick?target:name).focus();
 }
 modal.addEventListener('cancel',e=>{if(saving)e.preventDefault();});
 return {index,detail,collectionDetail,hide,
  add:(ids,options={})=>editDialog({...options,ids,pick:true}),
  fromImport:id=>editDialog({importJob:id}),
  async action(action,ids){
   if(action==='album'){editDialog({ids,pick:true});return;}
   const aid=current?.id;if(!aid)return;
   if(action==='transfer'){editDialog({ids,pick:true,moveFrom:aid});return;}
   await command(async()=>{
    if(action==='remove'){
     await api(`/api/albums/${aid}/remove`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({media_ids:ids})});
    }else if(ids.length===1){
     if(!await confirmAction(action==='cover'?'Use the selected photo as this album cover? This change is not available through Undo.':'Change this item\'s position in the album? This change is not available through Undo.',{title:action==='cover'?'Change album cover?':'Reorder album?',confirmLabel:'Apply'}))return;
     await api(`/api/albums/${aid}/items/${ids[0]}/${action==='cover'?'cover':'move'}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(action==='cover'?{}:{direction:action})});
    }
    await refresh();if(current?.id===aid)await detail(aid);
   });
  }
 };
}
