"""Validate explicit server folders without following directory links."""
from pathlib import Path


def source_folder(config, value):
    if str(value).startswith(('\\\\?\\','\\\\.\\')):
        raise ValueError('Device paths are not supported; use a drive folder or UNC share')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Choose an absolute folder on the server')
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Linked source folders are not allowed')
    if not path.is_dir():
        raise ValueError('Source folder is missing or inaccessible to the server')
    resolved = path.resolve()
    if config.get('destination', {}).get('root'):
        destination = Path(config['destination']['root']).resolve()
        if resolved == destination or resolved in destination.parents or destination in resolved.parents:
            raise ValueError('Source and destination must be separate')
    return resolved
