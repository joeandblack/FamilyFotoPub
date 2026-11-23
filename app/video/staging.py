"""Bounded cleanup of converter-owned scratch files, never enrolled media."""
from pathlib import Path
import re
import stat


NAMES = re.compile(r'(?:input\.(?:avi|flv|mts)(?:\.partial)?|output\.mp4|previous-[0-9]+\.mp4|'
                   r'(?:(?:flv|mts)-plan|frame-validation)\.json(?:\.[0-9]+\.tmp)?)')


def _plain(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise OSError('Conversion staging contains a link or junction; cleanup stopped')
    return info


def files(data, media_id):
    if type(media_id) is not int or media_id <= 0:
        raise ValueError('Invalid conversion media ID')
    data = Path(data).absolute()
    stage = data / 'conversion-temp' / str(media_id)
    # Check ancestors before looking through a potentially redirected directory.
    for path in reversed((stage, *stage.parents)):
        try:
            info = _plain(path)
        except FileNotFoundError:
            return stage, []
        if not stat.S_ISDIR(info.st_mode):
            raise OSError('Conversion staging is not a directory')
    entries = list(stage.iterdir())
    for path in entries:
        info = _plain(path)
        if not NAMES.fullmatch(path.name) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError('Unexpected conversion staging entry; cleanup stopped')
    return stage, entries


def cleanup(data, media_id, guard):
    if (Path(data) / 'video-decoder-busy.json').exists():
        raise OSError('Decoder still owns staging; cleanup deferred')
    stage, entries = files(data, media_id)
    for path in entries:
        guard()
        # Revalidate the complete bounded directory before each unlink.
        files(data, media_id)
        path.unlink(missing_ok=True)
    guard()
    if stage.exists():
        stage.rmdir()
