import {confirmAction} from './confirm-dialog.mjs';

export async function confirmAlbumTrash(api,ids,confirm=confirmAction){
 const {albums}=await api('/api/albums/membership',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({media_ids:ids})});
 if(!albums.length)return true;
 const names=albums.slice(0,5).map(album=>album.name).join(', ');
 return confirm(`Selected items belong to ${albums.length} album(s): ${names}${albums.length>5?' ...':''}. Moving to Trash hides them from Library and every album containing them. You can restore them from Recently Deleted.`,{title:'Move shared photos to Trash?',confirmLabel:'Move to Trash',danger:true});
}
