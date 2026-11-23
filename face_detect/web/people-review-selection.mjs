import {setupDragSelection} from '../../web/selection.mjs';
import {createPeopleActionProgress} from './people-review-progress.mjs';
import {bindPersonMenu} from '../../web/person-menu.mjs';

export function setupReviewSelection(grid,before,move,{people=()=>[],match,ignore,label='Unnamed groups to be reviewed'}={}){
 const toolbar=document.createElement('div');toolbar.className='review-selection-tools';
 const button=text=>{const b=document.createElement('button');b.type='button';b.textContent=text;toolbar.append(b);return b;};
 const toggle=button('Select'),all=button('Select all'),clear=button('Clear'),submit=button('Move to Review later');
 all.title='Select all groups on this page';
 const matchButton=button('Move to person');
 const ignoreButton=button('Ignore');ignoreButton.title='Ignore all faces in the selected groups; photos are not deleted';
 const status=document.createElement('span');status.className='muted';status.setAttribute('role','status');toolbar.append(status);before.before(toolbar);
 const indicator=createPeopleActionProgress(status,{inline:true});
 let enabled=false,busy=false,selected=new Set();
 const destinations=()=>{const seen=new Set();return people().filter(p=>p.person_id&&p.name&&!seen.has(p.person_id)&&seen.add(p.person_id));};
 const personMenu=bindPersonMenu(matchButton,destinations,pid=>perform((ids,report)=>match(ids,pid,report),'Moving groups...'));
 const cards=()=>[...grid.querySelectorAll('.person-card')];
 function paint(){
  grid.classList.toggle('selecting-groups',enabled);grid.setAttribute('aria-label',label);
  if(enabled){grid.setAttribute('role','listbox');grid.setAttribute('aria-multiselectable','true');}else{grid.removeAttribute('role');grid.removeAttribute('aria-multiselectable');}
  for(const card of cards()){
   const chosen=selected.has(Number(card.dataset.group));card.classList.toggle('group-selected',chosen);
   card.dataset.id=card.dataset.group;card.tabIndex=enabled?0:-1;
   if(enabled){card.setAttribute('role','option');card.setAttribute('aria-selected',String(chosen));}else{card.removeAttribute('role');card.removeAttribute('aria-selected');}
  }
  toggle.textContent=enabled?'Done selecting':'Select';toggle.disabled=busy;
  for(const b of [clear,submit])b.hidden=!enabled;
  submit.hidden=!enabled||!move;
  matchButton.hidden=!enabled||!match;
  matchButton.disabled=busy||!selected.size||!destinations().length;
  if(matchButton.hidden||matchButton.disabled)personMenu.close();
  ignoreButton.hidden=!enabled||!ignore;ignoreButton.disabled=busy||!selected.size;
  all.disabled=busy||!cards().length;clear.disabled=busy;submit.disabled=busy||!selected.size;
  if(!busy)status.textContent=enabled&&selected.size?`${selected.size} selected`:'';
  grid.setAttribute('aria-busy',String(busy));
 }
 // A click adds one group here, so a drag adds what it covers too.
 const drag=setupDragSelection(grid,{enabled:()=>enabled&&!busy,selected:()=>selected,change:value=>{selected=value;paint();},plain:'add'});
 function choose(event){
  if(!enabled)return;event.preventDefault();event.stopImmediatePropagation();
  const card=event.target.closest('.person-card');if(!card||busy)return;
  const id=Number(card.dataset.group);if(selected.has(id))selected.delete(id);else selected.add(id);paint();
 }
 grid.addEventListener('click',choose,true);
 grid.addEventListener('keydown',event=>{if(enabled&&[' ','Enter'].includes(event.key))choose(event);});
 toggle.onclick=()=>{drag.cancel();enabled=!enabled;selected.clear();paint();};
 all.onclick=()=>{drag.cancel();enabled=true;selected=new Set(cards().map(c=>Number(c.dataset.group)));paint();};
 clear.onclick=()=>{selected.clear();paint();};
 async function perform(action,label){
  if(busy||!selected.size)return;busy=true;drag.cancel();paint();
  indicator.start(label);
  const report=event=>indicator.report(event);
  try{
   const result=await action([...selected],report);
   const visible=new Set(cards().map(card=>Number(card.dataset.group)));
   selected=new Set((result?.remaining_group_ids||[]).filter(id=>visible.has(id)));
   enabled=selected.size>0;busy=false;paint();indicator.finish();
  }
  catch(error){busy=false;paint();indicator.fail(error);}
 }
 submit.onclick=()=>{if(move)return perform(move,'Moving groups...');};
 ignoreButton.onclick=()=>{if(ignore)return perform(ignore,'Ignoring faces...');};
 paint();
 return {toolbar,isBusy:()=>busy,clear(){drag.cancel();selected.clear();enabled=false;paint();},refresh(){personMenu.close();drag.cancel();const ids=new Set(cards().map(c=>Number(c.dataset.group)));selected=new Set([...selected].filter(id=>ids.has(id)));paint();}};
}
