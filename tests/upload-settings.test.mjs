import test from 'node:test';
import assert from 'node:assert/strict';
import {FIELDS,fromForm,toDisplay} from '../web/upload-settings.mjs';

const GIB=1024**3;
test('byte limits are shown in GiB and read back as whole bytes',()=>{
 const field=FIELDS.find(f=>f.key==='max_total_bytes');
 assert.equal(toDisplay(field,20*GIB),20);
 const raw=Object.fromEntries(FIELDS.map(f=>[f.key,String(toDisplay(f,{max_files:5000,max_file_bytes:5*GIB,max_total_bytes:20*GIB,staging_bytes:40*GIB,reserve_bytes:5*GIB,chunk_bytes:8*1024**2,expiry_hours:48,upload_mibps:12}[f.key]))]));
 assert.deepEqual(fromForm(raw),{max_files:5000,max_file_bytes:5*GIB,max_total_bytes:20*GIB,staging_bytes:40*GIB,reserve_bytes:5*GIB,chunk_bytes:8*1024**2,expiry_hours:48,upload_mibps:12});
});
test('a non-numeric entry is rejected with the field name',()=>{
 const raw=Object.fromEntries(FIELDS.map(f=>[f.key,'1']));raw.max_files='many';
 assert.throws(()=>fromForm(raw),/Files per upload/);
});
