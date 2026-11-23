// Shared status rules for Imports, collapsed summaries and Import overview.
const labels={'completed-review-required':'Complete',completed:'Complete','not-started':'Not started','paused-error':'Stopped with error',blocked:'Blocked',running:'Running',scanning:'Scanning',paused:'Paused',cancelled:'Cancelled',queued:'Queued','waiting-for-foreground':'Waiting for another operation','waiting-service':'Waiting for face detection service','awaiting-receipt':'Saving results',preparing:'Preparing scan'};
export const statusLabel=state=>labels[state]||state;
export function stageProgress(batch,stage){
 const counts=stage==='import'?batch.counts||{}:batch.stages?.[stage]||{};
 const total=Object.values(counts).reduce((a,b)=>a+b,0),pending=(counts.pending||0)+(counts.error||0);
 return {counts,total,pending,percent:total?(total-pending)/total*100:0};
}
export function importStageStatus(batch,stage,pipeline={}){
 if(batch.state==='cancelled')return 'Cancelled';
 const progress=stageProgress(batch,stage),matches=Number(pipeline.job_id)===Number(batch.id);
 const handoff=pipeline.handoff;
 if(handoff?.state==='pending'&&Number(handoff.job_id)===Number(batch.id)&&handoff.stage===stage)return 'Waiting to start';
 if(pipeline.busy&&!pipeline.face_busy&&matches&&pipeline.stage===stage)return pipeline.pause_requested?'Pausing...':stage==='import'&&batch.state==='scanning'?'Scanning':'Running';
 if(stage==='import')return ['running','scanning'].includes(batch.state)?'Interrupted':statusLabel(batch.state)||'Not started';
 if(!progress.total)return batch.state==='completed-review-required'?'Not applicable':'Not ready';
 if(!progress.pending)return 'Complete';
 const status=batch[stage+'_status'];
 return status?.state==='running'?'Interrupted':statusLabel(status?.state)||((progress.counts.done||0)>0?'Ready to resume':'Not started');
}
