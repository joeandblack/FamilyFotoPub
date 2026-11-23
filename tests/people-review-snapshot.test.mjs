import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewSnapshot} from '../face_detect/web/people-review-snapshot.mjs';

test('bulk review captures only faces in the selected displayed groups',()=>{
 const groups=[{id:1,review_version:'one',faces:[{detection_id:10},{detection_id:11}]},
               {id:2,review_version:'two',faces:[{detection_id:20}]}];
 const snapshot=reviewSnapshot(groups,[1],true);
 assert.deepEqual(snapshot,{group_versions:{'1':'one'},group_faces:{'1':[10,11]}});
 groups[0].faces.push({detection_id:12});
 assert.deepEqual(snapshot.group_faces['1'],[10,11]);
 assert.deepEqual(reviewSnapshot([{id:1,faces:[]}],[1],true),{});
});

test('review snapshot includes only selected groups and supports older servers', () => {
  const groups = [{id: 1, review_version: 'one'}, {id: 2, review_version: 'two'}];
  assert.deepEqual(reviewSnapshot(groups, [2]), {group_versions: {'2': 'two'}});
  assert.deepEqual(reviewSnapshot(groups, [3]), {});
  assert.deepEqual(reviewSnapshot([{id: 1}], [1]), {});
  assert.deepEqual(reviewSnapshot(groups, [1, 2]), {group_versions: {'1': 'one', '2': 'two'}});
});
