export const mediaURL=m=>`/api/media/${m.id}/${m.kind==='image'&&/\.(heic|heif)$/i.test(m.relative_path)?'preview':'original'}`;

// Retain only the current look-ahead window, never an album of decoded images.
export function createPhotoPreloader(makeImage=()=>new Image()){
  const entries=new Map();
  function load(m){
    if(m.kind!=='image')return Promise.resolve(null);
    const url=mediaURL(m);
    if(!entries.has(url)){
      const image=makeImage();image.alt=m.name;image.decoding='async';
      const ready=new Promise((resolve,reject)=>{
        image.onload=async()=>{
          try{await image.decode();resolve(image);}catch(error){reject(error);}
          finally{image.onload=image.onerror=null;}
        };
        image.onerror=()=>{image.onload=image.onerror=null;reject(Error('Preview unavailable'));};
        image.src=url;
      });
      // Speculative failures are reported only if the user reaches this photo.
      ready.catch(()=>{});entries.set(url,ready);
    }
    return entries.get(url);
  }
  return {load,clear:()=>entries.clear(),warm(items){
    const photos=items.slice(0,2).filter(m=>m.kind==='image'),keep=new Set(photos.map(mediaURL));
    for(const key of entries.keys())if(!keep.has(key))entries.delete(key);
    for(const m of photos)load(m);
  }};
}
