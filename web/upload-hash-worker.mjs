import {SHA256} from './sha256.mjs';
let hash;
self.onmessage=({data})=>{try{
 if(data.action==='start'){hash=new SHA256();self.postMessage({ok:true});}
 else if(data.action==='chunk'){const bytes=new Uint8Array(data.bytes);hash.update(bytes);self.postMessage({sha256:new SHA256().update(bytes).hex()});}
 else if(data.action==='finish'){self.postMessage({sha256:hash.hex()});hash=null;}
}catch(error){self.postMessage({error:error.message});}};
