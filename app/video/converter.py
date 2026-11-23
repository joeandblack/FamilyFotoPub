"""Explicit format dispatch with the existing single-worker conversion lifecycle."""
from pathlib import Path


def convert_one(db, media, config, tools, monitor):
    extension = Path(media.get('original_relative_path') or media['relative_path']).suffix.lower()
    if extension == '.avi':
        from .avi import convert_one as handler
    elif extension == '.flv':
        from .flv import convert_one as handler
    elif extension == '.mts':
        from .mts import convert_one as handler
    else:
        raise ValueError('Unsupported conversion input; original retained')
    return handler(db, media, config, tools, monitor)
