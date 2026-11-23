"""Backup dependency shared by the restore engine and installer preflight."""


def needs_backup(stages):
    if not isinstance(stages, dict):
        raise ValueError('Invalid restore stages')
    return 'thumbnails' not in stages and 'people' not in stages
