import test from 'node:test';
import assert from 'node:assert/strict';
import {peopleSection} from '../face_detect/web/people-sections.mjs';
test('named people, new unnamed groups and legacy Other photos stay separate',()=>{
 assert.equal(peopleSection({person_id:1,section:'review'}),'people');
 assert.equal(peopleSection({person_id:null,section:'review',optional_outlier:true}),'review');
 assert.equal(peopleSection({person_id:null,section:'other'}),'other');
 assert.equal(peopleSection({person_id:null,optional_outlier:false}),'review');
});
