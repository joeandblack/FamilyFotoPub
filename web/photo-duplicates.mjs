import {confirmAction,showNotice} from './confirm-dialog.mjs';
import {deleteWithProgress} from './deletion-progress.mjs';
export function setupPhotoDuplicateReview(api){
  const dialog=document.createElement('dialog');dialog.className='duplicate-review';
  dialog.setAttribute('aria-label','Photo duplicate review');document.body.append(dialog);
  let job,page=1,busy=false,mode='matches';
  const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  async function load(){
    const data=await api(`/api/import/${job}/photo-duplicates${mode==='trashed'?'/trashed':''}?page=${page}`);
    if(page>data.pages){page=Math.max(1,data.pages);return load();}
    dialog.replaceChildren();
    const header=el('div');header.className='duplicate-review-heading';
    header.append(el('h2','Photo duplicate review'));
    const close=el('button','Close');close.onclick=()=>dialog.close();header.append(close);dialog.append(header);
    const tabs=el('div');tabs.className='duplicate-review-tabs';
    for(const [label,value] of [['Matches','matches'],['Trashed duplicates','trashed']]){
      const button=el('button',label);button.disabled=mode===value;button.onclick=()=>{mode=value;page=1;load().catch(e=>showNotice(e.message));};tabs.append(button);
    }
    dialog.append(tabs);
    const selected=new Set(),selectors=[];
    let selectAll,bulk;
    const syncSelection=()=>{
      if(!bulk)return;
      selectAll.checked=selectors.length>0&&selected.size===selectors.length;
      selectAll.indeterminate=selected.size>0&&selected.size<selectors.length;
      bulk.disabled=busy||!selected.size;bulk.textContent=`Permanently delete selected (${selected.size})`;
    };
    if(mode==='trashed'&&data.items?.length&&document.body.dataset.role==='admin'){
      const toolbar=el('div');toolbar.className='duplicate-review-tabs duplicate-trash-selection';
      const label=el('label');selectAll=el('input');selectAll.type='checkbox';
      selectAll.setAttribute('aria-label','Select all trashed photos on this page');
      selectAll.onchange=()=>{for(const input of selectors){input.checked=selectAll.checked;if(input.checked)selected.add(input.mediaId);else selected.delete(input.mediaId);}syncSelection();};
      label.append(selectAll,document.createTextNode('Select all on this page'));
      bulk=el('button');bulk.className='write-action';
      bulk.onclick=async()=>{
        if(busy||!selected.size)return;
        const ids=[...selected];
        if(!await confirmAction(`Permanently delete ${ids.length} selected trashed photo(s) on this page?\n\nThis deletes their server copies and cannot be undone or restored. Kept photos and source files will not be deleted. Original SHA-256 records remain, so identical files will still be skipped on future imports.\n\nDeletion runs one file at a time. If an error occurs, it stops; files already deleted cannot be recovered here.`,{title:'Permanently delete duplicates?',confirmLabel:'Delete permanently',danger:true}))return;
        busy=true;
        const controls=[...dialog.querySelectorAll('button,input')];
        controls.forEach(control=>control.disabled=true);
        const preventClose=event=>event.preventDefault();dialog.addEventListener('cancel',preventClose);
        try{await deleteWithProgress({ids,api,refresh:load,elements:id=>[...dialog.querySelectorAll('[data-trashed-id]')].filter(node=>Number(node.dataset.trashedId)===id)});}
        finally{
          busy=false;dialog.removeEventListener('cancel',preventClose);
          controls.forEach(control=>control.disabled=false);syncSelection();
        }
      };
      toolbar.append(label,bulk);dialog.append(toolbar);syncSelection();
    }
    if(!data.total)dialog.append(el('p',mode==='trashed'?'No trashed duplicates.':'No matching photo groups found.'));
    const groups=el('div');groups.className='duplicate-review-columns';dialog.append(groups);
    for(const item of data.items||[]){
      const section=el('section');section.className='duplicate-review-group';
      section.dataset.trashedId=item.id;
      if(bulk){
        const label=el('label'),input=el('input');input.type='checkbox';input.mediaId=item.id;
        input.setAttribute('aria-label',`Select trashed photo ${item.name}`);
        input.onchange=()=>{if(input.checked)selected.add(item.id);else selected.delete(item.id);syncSelection();};
        selectors.push(input);label.append(input,document.createTextNode('Select'));section.append(label);
      }
      const pair=el('div');pair.className='duplicate-members duplicate-trash-pair';
      for(const [label,id,name] of [['Trashed',item.id,item.name],['Kept',item.preferred_id,item.preferred_name]]){
        const card=el('figure'),image=el('img');image.className='duplicate-trashed-image';image.src=`/api/media/${id}/thumbnail`;image.alt=name;image.loading='lazy';
        const link=el('a');link.href=`/api/media/${id}/original`;link.target='_blank';link.rel='noopener';link.title='Open full image';link.append(image);
        card.append(el('strong',label),link,el('figcaption',`${name} · #${id}`));pair.append(card);
      }
      section.append(pair);
      const restore=el('button','Restore');restore.className='write-action';restore.onclick=async()=>{
        if(busy)return;busy=true;restore.disabled=true;
        try{await api(`/api/import/${job}/photo-duplicates/trashed/${item.id}/restore`,{method:'POST'});await load();}
        catch(error){await showNotice(error.message);restore.disabled=false;}finally{busy=false;}
      };
      section.append(restore);
      if(document.body.dataset.role==='admin'){
        const remove=el('button','Permanently delete');remove.className='write-action';
        remove.onclick=async()=>{
          if(busy||!await confirmAction(`Permanently delete "${item.name}"? This cannot be undone. The kept photo will not be deleted.`,{title:'Permanently delete photo?',confirmLabel:'Delete permanently',danger:true}))return;
          busy=true;remove.disabled=true;restore.disabled=true;
          try{await deleteWithProgress({ids:[item.id],api,refresh:load,elements:()=>[section]});}
          finally{busy=false;remove.disabled=false;restore.disabled=false;}
        };
        section.append(remove);
      }
      groups.append(section);
    }
    for(const group of data.groups||[]){
      const section=el('section');section.className='duplicate-review-group';
      section.append(el('h3',`${group.decision==='candidate'?'To review':group.decision==='confirmed'?'Confirmed duplicates':'Keep separate'} · ${group.members.length} photos`),el('p',group.method));
      const members=el('div');members.className='duplicate-members';
      const select=el('select');select.setAttribute('aria-label','Preferred copy');
      for(const m of group.members){
        const card=el('figure');const image=el('img');image.src=`/api/media/${m.id}/thumbnail`;image.alt=m.name;image.loading='lazy';
        const link=el('a');link.href=`/api/media/${m.id}/original`;link.target='_blank';link.rel='noopener';link.title='Open full image';link.append(image);
        const caption=el('figcaption',`${m.name} · #${m.id}`);
        const evidence=m.evidence;
        const details=el('details');details.append(el('summary','Metadata'),el('p',`Camera: ${evidence.camera||'Unknown'} · GPS: ${evidence.gps||'Absent'} · Orientation: ${evidence.orientation}`),el('p',`Metadata fingerprint: ${evidence.exif_sha256.slice(0,12)}`));
        card.append(link,caption,el('p',`${m.size.toLocaleString()} bytes · Capture: ${m.captured_at||evidence.date||'Unknown'}`),details);members.append(card);
        const option=el('option',`${m.name} (#${m.id})`);option.value=m.id;select.append(option);
      }
      select.value=group.preferred_id;
      section.append(members,el('label','Preferred copy '),select);
      const save=async decision=>{
        if(busy)return;busy=true;
        try{await api(`/api/import/${job}/photo-duplicates/decision`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:group.key,decision,preferred_id:Number(select.value),revision:group.revision,member_ids:group.members.map(m=>m.id)})});await load();}
        catch(error){await showNotice(error.message);}finally{busy=false;}
      };
      for(const [label,value] of [['Confirm duplicates','confirmed'],['Keep separate','separate'],['Reset decision','candidate']]){
        const button=el('button',label);button.className='write-action';button.onclick=()=>save(value);section.append(button);
      }
      groups.append(section);
    }
    for(const warning of data.warnings||[])dialog.append(el('p',`${warning.name}: ${warning.error}`));
    const footer=el('div');footer.className='duplicate-review-heading';
    for(const [label,delta] of [['Previous',-1],['Next',1]]){
      const button=el('button',label);button.disabled=delta<0?page===1:page>=data.pages;
      button.onclick=()=>{page+=delta;load().catch(e=>showNotice(e.message));};footer.append(button);
    }
    footer.append(el('span',`${page} / ${data.pages}`));dialog.append(footer);
  }
  return async (id,initialMode='matches')=>{job=id;mode=initialMode;page=1;await load();if(!dialog.open)dialog.showModal();};
}
