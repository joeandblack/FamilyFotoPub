import test from 'node:test';
import assert from 'node:assert/strict';
import {videoDuration} from '../web/duration.mjs';
const video=duration_seconds=>({kind:'video',video_metadata:{duration_seconds}});
test('video durations use minutes and seconds, with hours for long clips',()=>{
  for(const [seconds,label] of [[0,'0:00'],[2.8,'0:02'],[33,'0:33'],[60,'1:00'],[1011,'16:51'],[3599.9,'59:59'],[3600,'1:00:00'],[3661,'1:01:01']])assert.equal(videoDuration(video(seconds)),label);
});
test('unknown durations and photos have no badge',()=>{
  for(const seconds of [null,undefined,'33',-1,Infinity,NaN,Number.MAX_SAFE_INTEGER+1])assert.equal(videoDuration(video(seconds)),'');
  assert.equal(videoDuration({kind:'video'}),'');
  assert.equal(videoDuration({kind:'image',video_metadata:{duration_seconds:33}}),'');
});
