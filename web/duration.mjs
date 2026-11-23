export function videoDuration(media) {
  const seconds = media.kind === 'video' ? media.video_metadata?.duration_seconds : null;
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0 || seconds > Number.MAX_SAFE_INTEGER) return '';
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const remainder = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${remainder}` : `${minutes}:${remainder}`;
}
