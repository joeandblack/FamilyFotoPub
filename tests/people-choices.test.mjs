import test from 'node:test';
import assert from 'node:assert/strict';
import {personChoices} from '../face_detect/web/people-choices.mjs';
test('existing people without visible faces remain choices, without duplicates',()=>{
 const state={groups:[{person_id:1,name:'Visible'},{person_id:null,name:null}],people:[{person_id:1,name:'Visible'},{person_id:3,name:'Ignored faces'}]};
 assert.deepEqual(personChoices(state).map(p=>p.person_id),[1,3]);
 assert.deepEqual(personChoices(null),[]);
 assert.equal(personChoices({groups:state.groups}).length,1);
});

test('global saved order wins over the current filtered group order',()=>{
 const state={groups:[{person_id:3,name:'Third'},{person_id:1,name:'First'}],people:[{person_id:2,name:'Second'},{person_id:1,name:'First'},{person_id:3,name:'Third'}]};
 assert.deepEqual(personChoices(state).map(p=>p.person_id),[2,1,3]);
});
