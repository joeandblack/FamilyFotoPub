export function createPeoplePhotoGallery({api,openViewer}) {
  const dialog=document.createElement('dialog');dialog.className='people-photo-gallery';
  dialog.setAttribute('aria-labelledby','people-photo-gallery-title');
  const heading=document.createElement('div');heading.className='section-heading';
  const title=document.createElement('h3');title.id='people-photo-gallery-title';title.textContent='Group photos';
  const close=document.createElement('button');close.type='button';close.title='Close';close.setAttribute('aria-label','Close group photos');
  close.innerHTML='<i data-lucide="x" aria-hidden="true"></i>';close.onclick=()=>dialog.close();
  heading.append(title,close);
  const status=document.createElement('p');status.className='muted';status.setAttribute('role','status');
  const grid=document.createElement('div');grid.className='grid';
  dialog.append(heading,status,grid);document.body.append(dialog);
  let ticket=0;
  dialog.addEventListener('close',()=>{ticket++;grid.replaceChildren();});
  dialog.addEventListener('keydown',event=>event.stopPropagation());
  return async (groupId,{url=null,name='Group photos'}={})=>{
    title.textContent=name;
    const current=++ticket;grid.replaceChildren();status.textContent='Loading photos...';status.classList.add('people-photos-loading');
    if(!dialog.open)dialog.showModal();globalThis.lucide?.createIcons();
    try{
      const result=await api(url||`/api/people-preview/groups/${groupId}/photos`);
      if(current!==ticket||!dialog.open)return;
      if(result.name)title.textContent=result.name;
      const items=[...new Map(result.items.map(item=>[item.id,item])).values()];
      status.textContent=items.length?`${items.length} photos`:'No available photos in this group';
      const tiles=items.map(item=>{
        const button=document.createElement('button');button.type='button';button.className='tile';button.title=item.name;button.setAttribute('aria-label',item.name);
        const frame=document.createElement('span');frame.className=`frame rotation-${item.rotation||0}`;
        const image=document.createElement('img');image.loading='lazy';image.decoding='async';image.alt='';
        image.src=`/api/media/${item.id}/thumbnail${item.crop?'?crop='+encodeURIComponent(JSON.stringify(item.crop)):''}`;
        frame.append(image);button.append(frame);
        button.onclick=()=>openViewer(item.id,{items,page:1,pages:1,filters:{kind:'image'}});
        return button;
      });
      grid.replaceChildren(...tiles);
    }catch(error){if(current===ticket)status.textContent=error.message;}
    finally{if(current===ticket)status.classList.remove('people-photos-loading');}
  };
}
