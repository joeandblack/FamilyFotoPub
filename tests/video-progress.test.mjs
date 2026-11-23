import { test } from 'node:test';
import assert from 'node:assert/strict';
import { videoProgress, videoForJob, createVideoEstimateDisplay, hoursMinutes } from '../web/progress.mjs';

test('video estimate uses current run throughput and refreshes every two minutes',()=>{
  const display=createVideoEstimateDisplay();
  const video={state:'running',started:'run-1',run_seconds:3600,run_processed:10,counts:{done:900,pending:20}};
  assert.equal(display(video,true,0),'About 2hr 0m left');
  video.run_processed=20;
  assert.equal(display(video,true,119999),'About 2hr 0m left');
  assert.equal(display(video,true,120000),'About 1hr 0m left');
  assert.equal(display({...video,state:'paused'},true,120001),'Paused');
  assert.equal(display(video,false,120002),'Not available');
  assert.equal(display({...video,state:'completed-review-required'},true,120003),'Complete');
  assert.equal(display({...video,started:'new-run',run_processed:0},true,120004),'Calculating...');
  assert.equal(hoursMinutes(3661),'1hr 1m');
});

test('Imports never borrows video counts from another batch or legacy global status', () => {
  assert.equal(videoForJob({counts:{done:3423}}, {id:3}),null);
  const video={counts:{done:3500},job_counts:{'1':{done:3423},'2':{done:77,pending:23}}};
  assert.equal(videoForJob(video,{id:3}),null);
  assert.equal(videoProgress(videoForJob(video,{id:2})).percent,77);
});

test('AVI phase shows conversion progress rather than completed metadata', () => {
  const progress=videoProgress({phase:'avi-conversion',counts:{done:100},conversion_counts:{done:2,warning:1,pending:7}});
  assert.equal(progress.percent,30);
  assert.match(progress.label,/Video conversions/);
});

test('processed includes ready, warnings and sidecars, not pending', () => {
  const progress = videoProgress({counts:{done:20,warning:2,ancillary:5,pending:73}});
  assert.equal(progress.processed,27);
  assert.equal(progress.total,100);
  assert.equal(progress.percent,27);
  assert.match(progress.label,/27\.0%/);
  assert.equal(progress.summary,'20 ready, 2 warnings, 5 sidecars, 73 pending');
});

test('paused worker retains progress; completion does not hide warnings', () => {
  const counts={done:2,warning:1,ancillary:5,pending:0};
  assert.equal(videoProgress({state:'paused-error',counts}).percent,100);
  assert.equal(videoProgress({state:'completed-review-required',counts}).done,2);
  assert.equal(videoProgress({counts}).warnings,1);
});

test('no job and empty or invalid counters are safe', () => {
  assert.equal(videoProgress(null),null);
  assert.equal(videoProgress({}).percent,0);
  assert.equal(videoProgress({counts:{done:-1,pending:NaN,warning:'3'}}).total,0);
});
