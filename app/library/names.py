"""Download names follow published formats without changing source identity."""
from pathlib import PureWindowsPath


def download_name(media):
    name=PureWindowsPath(media['name']).name
    if (media.get('kind')=='video' and media.get('original_relative_path')
            and PureWindowsPath(media['relative_path']).suffix.lower()=='.mp4'):
        name=str(PureWindowsPath(name).with_suffix('.mp4'))
    return name
