"""FLV-specific codec planning. Originals are never cleaned up by conversion."""
PROFILE = {'version': 1, 'video_codec': 'libx264', 'crf': 20, 'preset': 'medium',
           'pixel_format': 'yuv420p', 'audio_codec': 'aac', 'audio_bitrate': '128k',
           'preserve_original': True, 'frame_timing': 'passthrough'}


def plan(probe):
    video = [s for s in probe['streams'] if s['codec_type'] == 'video']
    audio = [s for s in probe['streams'] if s['codec_type'] == 'audio']
    if len(video) != 1 or len(audio) > 1:
        raise ValueError('FLV stream layout requires review; original retained')
    v = video[0]
    if not v.get('width') or not v.get('height') or v['width'] % 2 or v['height'] % 2:
        raise ValueError('FLV dimensions require review; original retained')
    if v.get('tags', {}).get('rotate', '0') not in ('0', 0) or any(
            s.get('rotation', 0) != 0 for s in v.get('side_data_list', [])):
        raise ValueError('FLV display rotation requires review; original retained')
    copy_video = v['codec_name'] == 'h264' and v.get('pix_fmt') == 'yuv420p'
    copy_audio = not audio or audio[0]['codec_name'] == 'aac'
    method = 'remux' if copy_video and copy_audio else 'audio-only' if copy_video else 'transcode'
    return {'profile': dict(PROFILE), 'method': method,
            'video': 'copy' if copy_video else 'libx264',
            'audio': ('copy' if copy_audio else 'aac') if audio else None}


def command(tools, source, output, selected):
    args = [tools['ffmpeg'], '-nostdin', '-n', '-v', 'error', '-xerror',
            '-protocol_whitelist', 'file', '-format_whitelist', 'flv', '-noautorotate',
            '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '0',
            '-c:v', selected['video']]
    if selected['video'] != 'copy':
        args += ['-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p',
                 '-threads', '2', '-fps_mode', 'passthrough']
    if selected['audio']:
        args += ['-c:a', selected['audio']]
        if selected['audio'] != 'copy':args += ['-b:a', '128k']
    return args + ['-movflags', '+faststart', str(output)]


def convert_one(db, media, config, tools, monitor):
    from .avi import convert_one as lifecycle
    return lifecycle(db, media, config, tools, monitor, flv_handler=True)
