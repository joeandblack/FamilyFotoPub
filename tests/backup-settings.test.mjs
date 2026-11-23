import test from 'node:test';
import assert from 'node:assert/strict';
import {backupTotalTime,durationText,estimateText,nextStepsText,restoreOutcome,restoreRunning,restoreStatusText,runOutcome,startBackupSections} from '../web/backup-settings.mjs';

test('backup history total spans start through finish including restart',()=>{
 const run={state:'finished',started_at:'2026-11-19T22:35:29.615075-08:00',completed_at:'2026-11-20T08:24:57Z',finished_at:'2026-11-20T08:25:30.849209Z'};
 assert.equal(backupTotalTime(run),'1h 50m 1s');
 assert.equal(backupTotalTime({...run,gave_up:true}),'1h 50m 1s');
 assert.equal(backupTotalTime({...run,finished_at:'2026-11-21T08:25:30Z'}),'25h 50m 0s');
 for(const changes of [{state:'capturing'},{state:'failed'},{started_at:null},{finished_at:null},{finished_at:'bad'},{finished_at:run.started_at.replace('22:35','21:35')}])assert.equal(backupTotalTime({...run,...changes}),'\u2014');
 const start='2026-11-20T00:00:00Z';
 assert.equal(backupTotalTime({state:'finished',started_at:start,finished_at:start}),'0s');
 assert.equal(backupTotalTime({state:'finished',started_at:start,finished_at:'2026-11-20T00:02:03Z'}),'2m 3s');
});
import {appendLog,finishedText,progressPercent,progressText,returnShown} from '../web/backup-progress.mjs';
import {size} from '../web/size.mjs';
import {readFileSync} from 'node:fs';

const settingsSource=readFileSync(new URL('../web/backup-settings.mjs',import.meta.url),'utf8');

test('the start dialog says what happens, what to keep, and what a failure means',()=>{
  const plan={media_bytes:2*1024**3,database_bytes:1024**2,estimate:{seconds:3600}};
  const sections=startBackupSections(plan,{destination:'F:\\Backups',unprotected:false});
  assert.deepEqual(sections.map(section=>section.heading),['What happens','Before you start','If something goes wrong']);
  const text=sections.flatMap(section=>section.items).join(' ');
  for(const expected of [/stops for everyone/,/F:\\Backups/,new RegExp(size(plan.media_bytes).replace('.','\\.')),/about 60 minutes/,/starts again by itself/,/administrator password protects the catalog/,/even if you change it later/])assert.match(text,expected);
  assert.doesNotMatch(text,/no file permissions/);
  const unprotected=startBackupSections({media_bytes:1,database_bytes:1},{destination:'F:',unprotected:true}).flatMap(section=>section.items).join(' ');
  assert.match(unprotected,/no file permissions/);assert.match(unprotected,/many hours/);
});
test('the log keeps its newest whole lines',()=>{
  assert.equal(appendLog('a\n','b\n'),'a\nb\n');
  assert.equal(appendLog('first line\n','second\nthird\n',12),'third\n');
});
test('the way back to FamilyFoto shows once the backup finished and the app came back',()=>{
  assert.equal(returnShown({finished:true,authorized:true}),true);
  assert.equal(returnShown({finished:false,authorized:true}),false);
  assert.equal(returnShown({finished:true,authorized:true,restart_error:'x'}),false);
  assert.equal(returnShown({finished:true,authorized:false}),false);
});

test('only a verified run is called verified',()=>{
  assert.equal(runOutcome({state:'finished',verified_at:'t'}),'Verified');
  assert.equal(runOutcome({state:'finished',gave_up:true}),'Failed, not a recovery point');
  assert.equal(runOutcome({state:'finished'}),'Not verified');
  assert.equal(runOutcome({state:'capturing'}),'Backing up');
});

test('a restore only says it worked when it finished',()=>{
  assert.equal(restoreOutcome({state:'finished'}),'Restored and checked');
  assert.equal(restoreOutcome({state:'validating'}),'Checking the restored copy');
  assert.equal(restoreOutcome({state:'failed'}),'Failed');
  assert.equal(restoreRunning({state:'restoring'}),true);
  assert.equal(restoreRunning({state:'finished'}),false);
  assert.equal(restoreRunning({state:'failed'}),false);
});

test('progress is described from what the capture reports',()=>{
  assert.equal(progressText(null),'');
  assert.equal(progressText({phase:'state',bytes:1024}),'Saving application data: 1.0 KB');
  assert.equal(progressText({phase:'thumbnails',bytes:2048}),'Saving thumbnails: 2.0 KB');
  assert.equal(progressText({phase:'thumbnails-verify',bytes:2048}),'Checking saved thumbnails: 2.0 KB');
  for(const phase of ['thumbnails-inventory','copy-source-check','copy-paths','copy-inventory-check','copy-space','copy-folders']){
    assert.match(progressText({phase,entries:12}),/: 12 entries$/);
    assert.equal(progressPercent({phase,entries:12}),null);
  }
  assert.match(progressText({phase:'copy',files:1,total_files:2,bytes:1024,total_bytes:2048}),/1 of 2 files, 1\.0 KB of 2\.0 KB/);
  assert.equal(progressPercent({phase:'copy',bytes:1,total_bytes:2}),50);
  assert.match(progressText({phase:'verify',files:1,total_files:2,bytes:1024,total_bytes:2048}),/Checking 1 of 2 files, 1\.0 KB of 2\.0 KB/);
  assert.equal(progressPercent({phase:'verify',bytes:1,total_bytes:4}),25);
  assert.equal(restoreStatusText({state:'restoring'}),'Restoring…');
  assert.match(restoreStatusText({state:'restoring',progress:{phase:'verify',files:1,total_files:2,bytes:1,total_bytes:2}}),/^Restoring… Checking 1 of 2 files/);
  assert.equal(progressPercent({phase:'copy',bytes:0,total_bytes:0}),null);
  assert.equal(progressPercent({phase:'database',bytes:1}),null);
});

test('the finish message never overstates the result',()=>{
  assert.match(finishedText({verified_at:'t'}),/verified/);
  assert.match(finishedText({gave_up:true}),/not a recovery point/);
  assert.match(finishedText({verified_at:'t',restart_error:'Could not restart'}),/Could not restart/);
});

test('the expected time is shown in plain units and says where it comes from',()=>{
  assert.equal(durationText(20),'1 minute');
  assert.equal(durationText(45*60),'45 minutes');
  assert.equal(durationText(5*3600),'5 hours');
  assert.match(estimateText({seconds:7200,last_seconds:3600,from_media_bytes:1024}),/last backup, of 1\.0 KB, took 60 minutes, so expect about 2 hours/);
  assert.match(estimateText(null),/no earlier backup.*3 to 14 hours/);
});

test('after a verified backup the page says what to carry, and never before',()=>{
  assert.equal(nextStepsText([]),'');
  assert.equal(nextStepsText([{state:'finished',directory:'F:\b'}]),'');
  assert.equal(nextStepsText([{state:'capturing'},{state:'finished',verified_at:'t',directory:'F:\b'}]).includes('F:\b'),true);
  assert.match(nextStepsText([{state:'finished',verified_at:'t',directory:'F:\b'}]),/administrator password this backup was made with/);
  assert.doesNotMatch(nextStepsText([{state:'finished',verified_at:'t',directory:'F:\b'}]),/recovery key/);
});

test('the backup asks for the administrator password once, hidden, and never for a key',()=>{
  assert.match(settingsSource,/id="backupPassword" type="password" autocomplete="current-password"/);
  assert.match(settingsSource,/id="showBackupPassword" type="checkbox"/);
  assert.match(settingsSource,/id="restorePassword" type="password"/);
  for(const gone of [/backupKeyFile/,/createBackupKey/,/\/api\/backup\/key/,/key_file/,/id="[^"]*Confirm[^"]*"/])assert.doesNotMatch(settingsSource,gone);
  // Sent only in request bodies, and cleared after start succeeds or fails.
  assert.match(settingsSource,/password:\$\('backupPassword'\)\.value/);
  assert.match(settingsSource,/finally\{clearInterval\(timer\);if\(!planRejected\)\$\('backupPassword'\)\.value='';\}/);
  assert.match(settingsSource,/retainOnConfirm:true/);
  assert.doesNotMatch(settingsSource,/location\.assign\('\/backup-progress'\)/);
  assert.match(settingsSource,/\$\('restorePassword'\)\.value='';/);
});
