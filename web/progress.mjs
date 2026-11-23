export function importDuplicates(batch) {
  const counts = batch.counts || {}, bytes = batch.bytes_by_state || {};
  return {
    count: (counts.duplicate || 0) + (counts.skipped_duplicate || 0),
    bytes: (bytes.duplicate || 0) + (bytes.skipped_duplicate || 0)
  };
}

export function videoForJob(video, job) {
  const counts = video?.job_counts?.[String(job?.id)];
  if (!counts) return null;
  return { ...video, counts, phase: 'metadata', conversion_counts: null };
}

export function importEstimate(job,now=Date.now()){
  if(job?.state!=='running'||!job.inventory_complete)return null;
  const seconds=job.active_seconds,files=job.processed,total=job.total;
  if(!Number.isFinite(seconds)||seconds<60||!(files>=10)||!(total>files))return null;
  const fileRemaining=seconds*(total-files)/files;
  const bytes=job.processed_bytes,allBytes=job.total_bytes;
  const byteRemaining=bytes>0&&allBytes>bytes?seconds*(allBytes-bytes)/bytes:0;
  const remaining=Math.max(fileRemaining,byteRemaining);
  if(!Number.isFinite(remaining)||remaining<=0)return null;
  return {remainingSeconds:remaining,completion:now+remaining*1000};
}

export function estimateLabel(completion,now=Date.now()){
  const minutes=Math.max(0,Math.ceil((completion-now)/60000));
  return `Est. ${Math.floor(minutes/60)}hr ${minutes%60}m left`;
}

export function createEstimateDisplay(){
  let key=null,updated=-Infinity,label='';
  return (job,healthy,now=Date.now())=>{
    const nextKey=`${job?.id}:${job?.state}:${healthy}`;
    if(key===nextKey&&now-updated<120000)return label;
    key=nextKey;updated=now;
    const estimate=healthy?importEstimate(job,now):null;
    label=estimate?estimateLabel(estimate.completion,now):
      job?.state==='completed-review-required'?'Import complete':
      job?.state==='paused'?'Estimate paused':
      job?.state==='running'&&healthy?'Est. calculating...':'';
    return label;
  };
}

export function videoProgress(video) {
  if (!video) return null;
  const conversion = video.phase === 'avi-conversion';
  const count = key => {
    const value = (conversion ? video.conversion_counts : video.counts)?.[key];
    return Number.isSafeInteger(value) && value > 0 ? value : 0;
  };
  const done = count('done'), warnings = count('warning');
  const sidecars = count('ancillary'), pending = count('pending');
  const processed = done + warnings + sidecars;
  const total = processed + pending;
  const percent = total ? processed / total * 100 : 0;
  const number = n => n.toLocaleString();
  return {
    done, warnings, sidecars, pending, processed, total, percent,
    label: `${conversion ? 'Video conversions' : 'Videos processed'}: ${number(processed)} / ${number(total)} - ${percent.toFixed(1)}%`,
    summary: `${number(done)} ready, ${number(warnings)} warnings, ${number(sidecars)} sidecars, ${number(pending)} pending`
  };
}
export function hoursMinutes(seconds){
  if(!Number.isFinite(seconds)||seconds<0)return 'Not available';
  const minutes=Math.floor(seconds/60);
  return `${Math.floor(minutes/60)}hr ${minutes%60}m`;
}

export function createVideoEstimateDisplay(){
  let key=null,updated=-Infinity,label='';
  return (video,healthy,now=Date.now())=>{
    const progress=videoProgress(video);
    const nextKey=`${video?.started}:${video?.state}:${healthy}:${progress?.pending===0}`;
    if(key===nextKey&&now-updated<120000)return label;
    key=nextKey;updated=now;
    if(video?.state==='completed-review-required')return label='Complete';
    if(video?.state==='paused')return label='Paused';
    if(video?.state!=='running'||!healthy)return label='Not available';
    const seconds=video.estimate_seconds??video.run_seconds,processed=video.run_processed;
    if(!Number.isFinite(seconds)||seconds<60||!Number.isFinite(processed)||processed<3||!progress?.pending)return label='Calculating...';
    const remaining=seconds/processed*progress.pending;
    return label=`About ${hoursMinutes(Math.ceil(remaining/60)*60)} left`;
  };
}

