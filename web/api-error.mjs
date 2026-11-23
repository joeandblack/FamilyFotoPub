export function apiErrorMessage(data,fallback='Request failed'){
 const detail=data?.detail??data?.error;
 if(typeof detail==='string'&&detail.trim())return detail;
 if(Array.isArray(detail)){
  const messages=detail.map(item=>{
   if(typeof item==='string')return item;
   if(typeof item?.msg!=='string')return '';
   const field=Array.isArray(item.loc)?item.loc.filter(value=>value!=='body').join('.'):'';
   return field?`${field}: ${item.msg}`:item.msg;
  }).filter(Boolean);
  if(messages.length)return messages.join('; ');
 }
 if(typeof detail?.message==='string')return detail.message;
 return fallback;
}
