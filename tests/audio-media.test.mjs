import assert from 'node:assert/strict';
import {isAudio} from '../web/audio-media.mjs';
assert(isAudio({name:'Music.MP3'}));
assert(isAudio({name:'misnamed.mp4',video_metadata:{media_type:'audio'}}));
assert(!isAudio({name:'movie.mp4',video_metadata:{media_type:'video'}}));
assert(!isAudio({name:'wrong.mp3',video_metadata:{media_type:'video'}}));
assert(!isAudio({name:'photo.jpg'}));
console.log('Audio type markers passed');
