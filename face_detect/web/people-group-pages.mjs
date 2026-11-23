import {pageSizeControl} from '../../web/page-size.mjs';
export const GROUP_PAGE_SIZE=100;
const renderedGroups=new WeakMap();
export function syncGroupCards(grid,groups,render){
 const existing=new Map([...grid.children].map(node=>[Number(node.dataset.group),node]));
 const nodes=groups.map(group=>{
  const signature=JSON.stringify(group);let node=existing.get(group.id);
  if(!node||renderedGroups.get(node)!==signature){node=render(group);renderedGroups.set(node,signature);}
  return node;
 });
 const wanted=new Set(nodes);
 for(const node of [...grid.children])if(!wanted.has(node))node.remove();
 nodes.forEach((node,index)=>{if(grid.children[index]!==node)grid.insertBefore(node,grid.children[index]||null);});
}
export function groupPage(groups,page,pageSize=GROUP_PAGE_SIZE){
 const pages=Math.max(1,Math.ceil(groups.length/pageSize));
 page=Math.max(1,Math.min(pages,Math.trunc(Number(page))||1));
 const start=(page-1)*pageSize;
 return {page,pages,total:groups.length,start,items:groups.slice(start,start+pageSize)};
}

export function setupGroupPages(grid,{render,selection,label,load}){
 let groups=[],page=1,pageSize=GROUP_PAGE_SIZE,remote=null;
 const nav=document.createElement('nav');nav.className='people-group-pages';nav.setAttribute('aria-label',`${label} pages`);
 const button=(icon,title)=>{const b=document.createElement('button');b.type='button';b.title=title;b.setAttribute('aria-label',title);b.innerHTML=`<i data-lucide="${icon}" aria-hidden="true"></i>`;return b;};
 const previous=button('chevron-left','Previous page'),next=button('chevron-right','Next page');
 const input=document.createElement('input');input.type='number';input.min='1';input.setAttribute('aria-label',`${label} page`);
 const total=document.createElement('span'),range=document.createElement('span');range.setAttribute('role','status');
 const size=pageSizeControl(pageSize,value=>{if(selection.isBusy()){size.querySelector('select').value=String(pageSize);return;}pageSize=value;page=1;selection.clear();if(load)load();else paint(true);},`${label} items per page`);
 nav.append(previous,input,total,next,range,size);grid.before(nav);
 function paint(resetScroll=false){
  const data=remote?{...remote,items:groups,start:(remote.page-1)*pageSize}:groupPage(groups,page,pageSize);page=data.page;
  syncGroupCards(grid,data.items,render);if(resetScroll)grid.scrollTop=0;selection.refresh();
  input.value=page;input.max=data.pages;total.textContent=`/ ${data.pages}`;
  range.textContent=`${data.total?data.start+1:0}-${data.start+data.items.length} / ${data.total} groups`;
  previous.disabled=page===1;next.disabled=page===data.pages;nav.hidden=!data.total;
  for(const control of [previous,input,total,next])control.hidden=data.pages===1;
  globalThis.lucide?.createIcons();
 }
 function go(value){if(selection.isBusy()){input.value=page;return;}page=Math.max(1,Math.min(remote?.pages||Infinity,Math.trunc(Number(value))||1));selection.clear();if(load)load();else paint(true);}
 previous.onclick=()=>go(page-1);next.onclick=()=>go(page+1);input.onchange=()=>go(input.value);
 return {reset(){page=1;remote=null;},parameters(){return {page,page_size:pageSize};},busy(value){nav.inert=value;grid.inert=value;if(selection.toolbar)selection.toolbar.inert=value;if(value){nav.hidden=false;range.textContent='Loading groups...';}else paint();},updatePage(data){remote=data;groups=data.groups;paint(true);},update(value){groups=value;paint();}};
}
