import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {findChrome,chromePath} from './chrome.mjs';

function withOverride(value,run){
 const before=process.env.FAMILYFOTO_CHROME;
 process.env.FAMILYFOTO_CHROME=value;
 try{return run();}finally{if(before===undefined)delete process.env.FAMILYFOTO_CHROME;else process.env.FAMILYFOTO_CHROME=before;}
}

test('FAMILYFOTO_CHROME overrides discovery when the file exists',()=>{
 const path=join(mkdtempSync(join(tmpdir(),'chrome-')),'chrome');writeFileSync(path,'');
 assert.equal(withOverride(path,findChrome),path);
 assert.equal(withOverride(path,chromePath),path);
});

test('a missing override is reported, not silently replaced',()=>{
 const missing=join(tmpdir(),'no-such-chrome');
 assert.equal(withOverride(missing,findChrome),null);
 assert.throws(()=>withOverride(missing,chromePath),/FAMILYFOTO_CHROME/);
});
