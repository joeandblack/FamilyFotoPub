import test from 'node:test';
import assert from 'node:assert/strict';
import {needsPlaybackReview,setupConversionReview} from '../web/conversion-review.mjs';

const video=id=>({id,kind:'video',name:`${id}.MTS`,original_relative_path:`${id}.mts`,conversion_state:'done',video_metadata:{poster:'poster.jpg',conversion_validation:{playback:'review-required'}}});
test('playback review uses durable validation evidence, not all conversion warnings',()=>{
 assert.equal(needsPlaybackReview(video(1)),true);
 assert.equal(needsPlaybackReview({...video(1),video_metadata:{conversion_validation:{playback:'user-reviewed',original_action:'keep'}}}),true);
 assert.equal(needsPlaybackReview({...video(1),video_metadata:{conversion_validation:{playback:'user-reviewed',original_deleted_at:'today'}}}),false);
 assert.equal(needsPlaybackReview({kind:'video',conversion_state:'warning'}),false);
 assert.equal(needsPlaybackReview({...video(1),kind:'image'}),false);
 assert.equal(needsPlaybackReview({kind:'video',video_metadata:{conversion_validation:{playback:'decoded'}}}),false);
});

test('review loads only the selected import, paginates 100 thumbnails and opens the actual video',async()=>{
 const old=globalThis.document;
 const make=tag=>({tag,children:[],style:{},classList:{},after(){},append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=nodes;},setAttribute(){},addEventListener(){},showModal(){this.open=true;},close(){this.open=false;}});
 globalThis.document={createElement:make,body:make('body')};
 try{
  const requests=[],opened=[],writes=[];
  const review=setupConversionReview({api:async (url,options)=>{
   if(options?.method==='POST'){writes.push(url);return {ok:true};}
   const query=new URL(url,'http://localhost').searchParams;requests.push(query);
   return query.get('page')==='1'?{pages:2,items:[...Array.from({length:100},(_,i)=>video(i+1)),{id:900,kind:'video'}]}:{pages:2,items:[video(101)]};
  },openViewer:(...args)=>opened.push(args)});
  await review({id:7});
  assert.equal(requests.length,2);assert.ok(requests.every(q=>q.get('import_job')==='7'&&q.get('kind')==='video'));
  const dialog=document.body.children[0],controls=dialog.children[3],grid=dialog.children[4];
  assert.equal(grid.children.length,100);controls.children[3].onclick();assert.equal(grid.children.length,1);
  grid.children[0].children[0].onclick();assert.equal(opened[0][0],101);assert.equal(opened[0][1].items.length,1);
  assert.equal(dialog.children[2].textContent,'101 videos with retained originals');
  const keep=controls.children.find(e=>e.textContent==='Delete retained originals');
  assert.equal(keep.disabled,true);
  controls.children.find(e=>e.textContent==='Select').onclick();
  grid.children[0].children[0].onclick();assert.equal(keep.disabled,false);
  controls.children.find(e=>e.textContent==='Done selecting').onclick();
  assert.deepEqual(writes,[]);
  assert.equal(keep.disabled,true);assert.equal(grid.children.length,1);
  assert.ok(grid.children.every(c=>!c.children[0].className.includes('selected')));
 }finally{globalThis.document=old;}
});
