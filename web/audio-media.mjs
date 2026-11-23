const audioExtension=/\.(mp3|m4a|m4b|aac|wav|flac|ogg|opus|wma|aif|aiff)$/i;
export function isAudio(media){
 const type=media.video_metadata?.media_type;
 if(type)return type==='audio';
 return !media.video_metadata?.video_codec&&audioExtension.test(media.name||'');
}
