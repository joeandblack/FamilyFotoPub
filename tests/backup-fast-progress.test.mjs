import test from 'node:test';
import assert from 'node:assert/strict';
import {progressText,progressPercent,retainedAttempts} from '../web/backup-progress.mjs';
import {backupOptions,copyMethodDescription,startBackupSections} from '../web/backup-settings.mjs';

test('separate copy and verification phases show independent timing and progress',()=>{
 const event={phase:'media-transfer',state:'completed',elapsed_seconds:480,files:4,total_files:4,bytes:2048,total_bytes:2048};
 assert.match(progressText(event),/Copying photos and videos: completed, 480.0 seconds/);
 assert.equal(progressPercent(event),100);
 const checking={...event,phase:'media-verification',state:'started',elapsed_seconds:0,files:0,bytes:0};
 assert.match(progressText(checking),/Verifying copied media: started, 0.0 seconds/);
 assert.equal(progressPercent(checking),0);
 assert.doesNotMatch(progressText(event),/Backup finished|backup complete/i);
 assert.equal(progressPercent({phase:'media-final-check',elapsed_seconds:1}),null);
});

test('worker exit wait is explicit and does not imply backup completion',()=>{
 const event={phase:'worker-exit',state:'waiting',elapsed_seconds:6};
 assert.match(progressText(event),/Waiting for the media worker/);
 assert.equal(progressPercent(event),null);
});

test('worker exit success still shows backup finalization pending',()=>{
 const event={phase:'worker-exited',elapsed_seconds:6.125,exit_code:0};
 assert.match(progressText(event),/finishing the backup/);
 assert.equal(progressPercent(event),null);
});

test('fast copy heartbeat stays visible without inventing a byte count',()=>{
 assert.match(progressText({phase:'copy-and-verify',state:'running'}),/Copying and checking/);
 assert.equal(progressPercent({phase:'copy-and-verify',state:'running'}),null);
});
test('fast copy reports file and byte progress',()=>{
 const event={phase:'copy-and-verify',files:2,total_files:4,bytes:1024,total_bytes:2048};
 assert.match(progressText(event),/2 of 4/);
 assert.equal(progressPercent(event),50);
});
test('retained attempts require authorization and retain literal folder names',()=>{
 const paths=['D:\\backup\\old','<img src=x onerror=alert(1)>','D:\\backup\\old',null,''];
 assert.deepEqual(retainedAttempts({authorized:false,retained_attempts:paths}),[]);
 assert.deepEqual(retainedAttempts({authorized:true,retained_attempts:paths}),paths.slice(0,2));
 assert.deepEqual(retainedAttempts({authorized:true}),[]);
});
test('Settings selection requests full verification and discloses new-attempt space',()=>{
 assert.deepEqual(backupOptions('fast-full'),{copy_strategy:'fast-full',verification:'full'});
 const text=startBackupSections(null,{destination:'F:\\Backups',unprotected:false,copyStrategy:'fast-full'}).flatMap(s=>s.items).join(' ');
 assert.match(text,/copies everything into another folder/);
 assert.match(text,/space for that whole copy/);
});
test('only Standard and Fast are selectable and both verify every file',()=>{
 assert.throws(()=>backupOptions('unknown'),/Standard or Fast/);
 for(const choice of ['standard','fast-full']){
  const text=startBackupSections(null,{destination:'F:/Backups',copyStrategy:choice}).flatMap(s=>s.items).join(' ');
  assert.match(text,/Every file in the copy is then read/);
  assert.equal(backupOptions(choice).verification,'full');
  assert.match(copyMethodDescription(choice),/every file/);
 }
 assert.match(copyMethodDescription('standard'),/Resumable/);
 assert.match(copyMethodDescription('fast-full'),/start a new copy/);
});
