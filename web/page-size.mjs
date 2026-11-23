export const PAGE_SIZES=[50,100,200,500,1000];
export function pageSizeControl(value,onChange,label='Items per page'){
 const wrapper=document.createElement('label');wrapper.className='page-size-control';wrapper.textContent='Per page';
 const select=document.createElement('select');select.setAttribute('aria-label',label);
 for(const size of PAGE_SIZES){const option=document.createElement('option');option.value=String(size);option.textContent=String(size);select.append(option);}
 select.value=String(value);select.onchange=()=>onChange(Number(select.value));wrapper.append(select);return wrapper;
}
export function reviewImportFromURL(search){
 const value=new URLSearchParams(search).get('import');
 return value&&/^\d+$/.test(value)&&Number.isSafeInteger(Number(value))&&Number(value)>0?Number(value):null;
}
