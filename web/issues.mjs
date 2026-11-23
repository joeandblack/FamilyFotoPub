export function renderIssues(container,data,{unsupportedHeading=true,emptyText='',openKeys=new Set(),jobId=null}={}){
  const document=container.ownerDocument;
  container.replaceChildren();
  if(data.imports){
    if(!data.imports.length){renderIssues(container,{unsupported:[],errors:[]});return;}
    for(const batch of data.imports){
      if(!batch.unsupported_count&&!batch.errors.length)continue;
      const details=document.createElement('details');details.className='issue-batch';
      details.open=batch.errors.length>0;
      const name=batch.source_root.replace(/\\/g,'/').replace(/\/$/,'').split('/').pop();
      const summary=document.createElement('summary');
      const counts=[];
      if(batch.unsupported_count)counts.push(`${batch.unsupported_count.toLocaleString()} unsupported files`);
      if(batch.errors.length)counts.push(`${batch.errors.length} failed files`);
      summary.textContent=`${name} (Import #${batch.job_id}) - ${counts.join(', ')}`;
      details.append(summary);
      const metadata=document.createElement('p');metadata.className='muted';
      metadata.textContent=`${batch.source_root} | ${new Date(batch.started_at).toLocaleString()} | ${batch.state}`;
      details.append(metadata);
      const body=document.createElement('div');renderIssues(body,batch);details.append(body);
      container.append(details);
    }
    return;
  }
  function file(row){
    const element=document.createElement('div');element.className='issue';
    const path=document.createElement('div');path.textContent=row.source_path;element.append(path);
    if(row.error){const error=document.createElement('small');error.textContent=row.error;element.append(error);}
    return element;
  }
  if(data.errors.length){
    const heading=document.createElement('h4');heading.textContent=`Failed files (${data.errors.length})`;container.append(heading);
    for(const row of data.errors)container.append(file(row));
  }
  if(data.unsupported.length&&unsupportedHeading){
    const heading=document.createElement('h4');heading.textContent='Unsupported files';container.append(heading);
  }
  for(const group of data.unsupported){
    const details=document.createElement('details');details.className='issue-group';
    const key=JSON.stringify([jobId,group.extension]);
    if(jobId!==null)details.dataset.issueKey=key;
    const summary=document.createElement('summary');summary.textContent=`${group.extension} (${group.count.toLocaleString()})`;
    details.append(summary);let populated=false;
    const populate=()=>{
      if(!details.open||populated)return;
      for(const row of group.files)details.append(file(row));
      populated=true;
    };
    details.addEventListener('toggle',populate);
    details.open=openKeys.has(key);populate();
    container.append(details);
  }
  if(emptyText&&!data.errors.length&&!data.unsupported.length){
    const empty=document.createElement('p');empty.className='muted';empty.textContent=emptyText;container.append(empty);
  }
}

export function importIssues(data,jobId){
  return data.imports?.find(batch=>batch.job_id===jobId)||{errors:[],unsupported:[],unsupported_count:0};
}

export function appendImportIssues(container,data,jobId,openKeys=new Set()){
  const doc=container.ownerDocument;
  if(data.errors.length){
    const errors=doc.createElement('section');errors.className='import-errors';
    renderIssues(errors,{errors:data.errors,unsupported:[]});container.append(errors);
  }
  const total=data.unsupported.reduce((count,group)=>count+group.count,0);
  if(!total)return;
  const unsupported=doc.createElement('section');unsupported.className='import-unsupported';
  const heading=doc.createElement('h4');
  heading.textContent=`Unsupported files (${total.toLocaleString()})`;unsupported.append(heading);
  const body=doc.createElement('div');
  renderIssues(body,{errors:[],unsupported:data.unsupported},{unsupportedHeading:false,openKeys,jobId});
  unsupported.append(body);container.append(unsupported);
}
