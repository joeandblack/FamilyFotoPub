import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
test('Back to People navigates directly without depending on the review frame',()=>{
 const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
 const start=source.indexOf('function openPeopleHome(){'),end=source.indexOf('peopleBack.onclick=openPeopleHome;',start);
 const review={hidden:true},frame={style:{},src:'/people-preview?import=22#review-faces'},back={hidden:false};
 const context={peopleBack:back,previewPanel:frame,peopleToolbar:{querySelector:()=>review},renderPeopleHeading(){},pageLoading:{start:()=>()=>{}}};
 vm.createContext(context);vm.runInContext(source.slice(start,end),context);context.openPeopleHome();
 assert.equal(frame.src,'/people-preview?desktop=1&jobs=1&integrated=1');
 assert.equal(frame.style.visibility,'hidden');assert.equal(back.hidden,true);assert.equal(review.hidden,false);
 assert.equal(context.peopleAwaitingUnfiltered,true);assert.equal(context.peopleReviewOpen,false);
 assert(source.includes('peopleBack.onclick=openPeopleHome;'));
});
