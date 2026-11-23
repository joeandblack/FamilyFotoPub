export function peopleWarningLabel(code){
 return {
  unsupported_media:'Image format or frame layout is not supported for face detection',
  corrupt_or_unsafe_media:'Image could not be decoded safely for face detection',
  source_ineligible_or_changed:'Photo changed or is no longer eligible for face detection',
 }[code]||code||'No details recorded';
}

export function peopleWarningDetail(warning){
 if(warning.detail)return warning.detail;
 return {
  unsupported_media:'The decoder rejected the image format or could not choose a supported primary frame. The exact reason was not saved by the earlier scan.',
  corrupt_or_unsafe_media:'Possible causes include damaged image data, a pixel-size safety limit, or an unusable embedded color profile. The exact reason was not saved by the earlier scan.',
  source_ineligible_or_changed:'The photo changed or became unavailable or excluded before it could be analyzed.',
 }[warning.error]||'';
}
