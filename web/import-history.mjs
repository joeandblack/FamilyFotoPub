import {size} from './size.mjs';
import {importDuplicates} from './progress.mjs';
export function renderImportHistory(container,batches){
  const document=container.ownerDocument;
  container.replaceChildren();
  const states={'completed-review-required':'Completed - review available','paused-error':'Stopped with error',running:'Importing',scanning:'Scanning',paused:'Paused',cancelled:'Cancelled'};
  for(const batch of batches){
    const row=document.createElement('div');row.className='import-history-row';
    const heading=document.createElement('strong');
    const name=batch.source_root.replace(/\\/g,'/').replace(/\/$/,'').split('/').pop();
    heading.textContent=`${name} #${batch.id} - ${states[batch.state]||batch.state}`;
    const meta=document.createElement('div');meta.className='muted';
    meta.textContent=`${batch.source_root} | ${new Date(batch.started_at).toLocaleString()}`;
    const totals=document.createElement('div');
    if(batch.state==='cancelled')totals.textContent='Cancelled import';
    else {
      const c=batch.counts,b=batch.bytes_by_state;
      const duplicates=importDuplicates(batch);
      totals.textContent=`${batch.total.toLocaleString()} files${batch.inventory_complete?'':' scanned so far'} / ${size(batch.total_bytes)} | Copied ${c.imported||0} (${size(b.imported||0)}) | Duplicates ${duplicates.count} (${size(duplicates.bytes)})${c.unsupported?` | Unsupported ${c.unsupported}`:''} | Pending ${c.pending||0} | Errors ${c.error||0}`;
    }
    row.append(heading,meta,totals);container.append(row);
  }
  if(!batches.length)container.textContent='No imports recorded.';
}
