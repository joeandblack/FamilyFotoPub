import test from 'node:test';
import assert from 'node:assert/strict';
import {buildLabel,buildTitle} from '../web/build-label.mjs';

const commit='9773f6e'+'0'.repeat(33);
test('sidebar build label shows version, branch and short commit',()=>{
 assert.equal(buildLabel({version:'0.1.0b1',branch:'main',commit,dirty:false}),'v0.1.0b1 · 9773f6e · main');
 assert.equal(buildLabel({version:'0.1.0b1',branch:'',commit,dirty:true}),'v0.1.0b1 · 9773f6e+');
 assert.equal(buildLabel({version:'unknown',branch:'',commit:'',dirty:false}),'');
 assert.equal(buildLabel(null),'');
 assert.equal(buildTitle({version:'0.1.0b1',branch:'main',commit,dirty:true}),`Version 0.1.0b1\nBranch main\nCommit ${commit} (built with local changes)`);
});
