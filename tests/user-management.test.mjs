import test from 'node:test';
import assert from 'node:assert/strict';
import {generatePassword,ROLE_LABELS,selfResetProblem} from '../web/user-management.mjs';

test('generated passwords are long, unambiguous and different each time',()=>{
 const seen=new Set();
 for(let i=0;i<200;i++){
  const password=generatePassword();
  assert.equal(password.length,16);
  assert.match(password,/^[A-HJ-NP-Za-km-z2-9]+$/);
  seen.add(password);
 }
 assert.equal(seen.size,200);
});

test('bytes that would bias the alphabet are skipped',()=>{
 // 255 is above the largest unbiased byte; only 0 (A) and 1 (B) may be used.
 const random={getRandomValues:array=>{array.forEach((_,i)=>array[i]=[255,0,255,1][i%4]);return array;}};
 assert.equal(generatePassword(4,random),'ABAB');
});

test('roles use the Settings names',()=>{
 assert.deepEqual(ROLE_LABELS,{admin:'Admin',regular:'Normal',guest:'Guest'});
});

test('resetting your own password needs the saved-password tick; others do not',()=>{
 assert.match(selfResetProblem('admin','admin',false),/tick the box/);
 assert.equal(selfResetProblem('admin','admin',true),null);
 assert.equal(selfResetProblem('guest','admin',false),null);
});
