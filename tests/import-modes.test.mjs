import test from 'node:test';
import assert from 'node:assert/strict';
import {stageProgress} from '../web/import-pipelines.mjs';
import {importDuplicates} from '../web/progress.mjs';

test('missing-only duplicates are displayed without losing normal duplicates',()=>{
  assert.deepEqual(importDuplicates({counts:{imported:43,pending:4,skipped_duplicate:9},bytes_by_state:{skipped_duplicate:151109094}}),{count:9,bytes:151109094});
  assert.deepEqual(importDuplicates({counts:{duplicate:2,skipped_duplicate:9},bytes_by_state:{duplicate:20,skipped_duplicate:90}}),{count:11,bytes:110});
  assert.deepEqual(importDuplicates({counts:{duplicate:2},bytes_by_state:{duplicate:20}}),{count:2,bytes:20});
  assert.deepEqual(importDuplicates({}),{count:0,bytes:0});
});

test('aggregate skipped duplicates remain in file progress',()=>{
  const batch={counts:{imported:1,skipped_duplicate:98,pending:1}};
  assert.equal(stageProgress(batch,'import').percent,99);
  assert.equal(stageProgress(batch,'import').total,100);
  assert.equal(stageProgress({counts:{skipped_duplicate:100}},'import').percent,100);
});
