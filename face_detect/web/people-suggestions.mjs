import {createPeopleActionProgress} from './people-review-progress.mjs';
export function setupPeopleSuggestions({request,endpoint,viewGroup,before,recheck}){
 const element=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 const section=element('details',undefined,'people-suggestions');section.id='possible-matches';
 const summary=element('summary'),summaryLabel=element('span','Possible matches (0)'),status=element('p'),grid=element('div',undefined,'suggestions-grid'),footer=element('div',undefined,'suggestion-pagination');
 summary.append(summaryLabel);
 if(recheck){
  recheck.style.marginInlineStart='18px';summary.append(recheck);
  recheck.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();});
 }
 status.setAttribute('role','status');section.append(summary,status,grid,footer);before.before(section);
 const indicator=createPeopleActionProgress(status);
 let page=1,ticket=0,busy=false,data=null,errorMessage='';
 function render(){
  if(!data)return;
  summaryLabel.textContent=`Possible matches (${data.total})`;footer.replaceChildren();
  if(!section.open)return;
  status.textContent=errorMessage||(data.total?'Review each face before confirming. Similarity is not a probability; only the shown face will be assigned.':'No pending suggestions. Detect again checks for possible matches.');
  const existing=new Map([...grid.children].map(card=>[card.dataset.key,card]));
  const retained=new Set();
  for(const item of data.items){
   const key=JSON.stringify(item),previous=existing.get(key);
   if(previous){retained.add(previous);continue;}
   const card=element('article',undefined,'suggestion-card'),pair=element('div',undefined,'suggestion-pair');
   card.dataset.key=key;retained.add(card);
   for(const [did,label] of [[item.detection_id,'Unassigned face'],[item.anchor_id,item.name]]){
    const figure=element('figure'),img=element('img');img.src=`${endpoint}/thumbnails/${did}`;img.width=76;img.height=84;img.loading='lazy';img.alt=label;
    figure.append(img,element('figcaption',label));pair.append(figure);
   }
   const actions=element('div',undefined,'suggestion-actions');
   for(const [decision,label] of [['confirm','Confirm match'],['reject','Not this person']]){
    const button=element('button',label);button.type='button';button.disabled=busy;
    button.onclick=async()=>{
     if(busy)return;busy=true;errorMessage='';indicator.start(decision==='confirm'?'Confirming match...':'Rejecting match...');for(const b of grid.querySelectorAll('button'))b.disabled=true;
     try{
      await request('/suggestions/decision',{revision:data.revision,run_id:item.run_id||data.run_id,detection_id:item.detection_id,person_id:item.person_id,decision});
      const remaining=data.items.filter(candidate=>decision==='confirm'?candidate.detection_id!==item.detection_id:!(candidate.detection_id===item.detection_id&&candidate.person_id===item.person_id));
      data={...data,total:data.total-(data.items.length-remaining.length),items:remaining};render();
      await load();
     }
     catch(error){errorMessage=error.message;}
     finally{busy=false;render();if(errorMessage)indicator.fail(errorMessage);else indicator.finish(status.textContent);}
    };actions.append(button);
   }
   const view=element('button','View group');view.type='button';view.onclick=()=>viewGroup(item.group_id);actions.append(view);
   card.append(pair,element('p',`Similarity ${item.score.toFixed(3)}`),actions);grid.append(card);
  }
  for(const card of [...grid.children])if(!retained.has(card))card.remove();
  const ordered=new Map([...grid.children].map(card=>[card.dataset.key,card]));
  data.items.forEach((item,index)=>{const card=ordered.get(JSON.stringify(item));if(grid.children[index]!==card)grid.insertBefore(card,grid.children[index]||null);});
  for(const button of grid.querySelectorAll('button'))button.disabled=busy;
  if(data.total){
   for(const [label,step] of [['Previous',-1],['Next',1]]){
    const button=element('button',label);button.type='button';button.disabled=busy||(step<0?page<=1:page>=data.pages);
    button.onclick=()=>{if(busy)return;page+=step;load();};footer.append(button);
   }
   footer.append(element('span',`${page} / ${data.pages}`));
  }
 }
 async function load(){
  const current=++ticket;
  try{const result=await request(`/suggestions?page=${page}`);if(current!==ticket)return;errorMessage='';data=result;page=result.page;render();}
  catch(error){if(current===ticket){errorMessage=error.message;status.textContent=errorMessage;section.open=true;}}
 }
 section.addEventListener('toggle',()=>{if(section.open)render();});
 return {reset(){page=1;ticket++;data=null;grid.replaceChildren();},refresh:load,async open(){section.open=true;await load();section.scrollIntoView({block:'nearest'});}};
}
