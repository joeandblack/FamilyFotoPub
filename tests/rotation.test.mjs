import test from 'node:test';
import assert from 'node:assert/strict';
import {rotationStyle} from '../web/rotation.mjs';
test('quarter turns swap the fitting box without clipping',()=>{
  assert.deepEqual(rotationStyle(90,1280,800),{width:'800px',height:'1280px',transform:'rotate(90deg)'});
  assert.deepEqual(rotationStyle(270,390,844),{width:'844px',height:'390px',transform:'rotate(270deg)'});
  assert.deepEqual(rotationStyle(180,390,844),{width:'390px',height:'844px',transform:'rotate(180deg)'});
  assert.deepEqual(rotationStyle(360,1280,800),rotationStyle(0,1280,800));
});
