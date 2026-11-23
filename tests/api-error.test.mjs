import test from 'node:test';
import assert from 'node:assert/strict';
import {apiErrorMessage} from '../web/api-error.mjs';

test('validation errors display field and message instead of object coercion',()=>{
 assert.equal(apiErrorMessage({detail:[{loc:['body','media_ids'],msg:'List should have at most 1000 items',input:[1,2]}]}),'media_ids: List should have at most 1000 items');
 assert.equal(apiErrorMessage({detail:'Album not found'}),'Album not found');
 assert.equal(apiErrorMessage({error:'Denied'}),'Denied');
 assert.equal(apiErrorMessage({detail:{message:'Unavailable'}}),'Unavailable');
 assert.equal(apiErrorMessage({detail:{input:'private'}}),'Request failed');
});
