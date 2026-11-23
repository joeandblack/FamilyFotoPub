export function createImportDetailLoader(api,now=()=>Date.now()){
 const requests=new Map();
 return id=>{
  const cached=requests.get(id);
  if(cached&&(cached.pending||now()-cached.updated<4500))return cached.promise;
  const entry={pending:true,updated:now()};
  entry.promise=Promise.all([api(`/api/import/history?job_id=${id}`),api(`/api/issues?job_id=${id}`)])
   .then(([result,issues])=>({...result,issues}))
   .catch(error=>{requests.delete(id);throw error;})
   .finally(()=>{entry.pending=false;entry.updated=now();});
  requests.set(id,entry);return entry.promise;
 };
}
