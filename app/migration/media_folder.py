"""Check the folder a restore puts the photos and videos in, before anything is installed or recorded.

A restore checked these only once it had begun, after the installer had installed Core and
recorded the choices. A folder on the wrong disk then left a restore that could neither go on
(the recorded choice was wrong) nor start again (Core was installed). Checked here, a wrong
folder is a question asked again, not a stuck installation.

Standard library only: the Mac installer asks before the app's own packages are installed.
"""
import os
from pathlib import Path

from ..backup.diagnostics import ExplainedFailure
from ..backup.private_storage import UnprotectedDestination, check_destination_privacy
from ..backup.volumes import volume_identity


class UnsuitableMediaFolder(ExplainedFailure):
    """`unprotected` is True when the only problem is a disk without permissions, which the
    person may accept."""

    def __init__(self, message, *, unprotected=False):
        super().__init__(message)
        self.unprotected = unprotected


def check_media_folder(backup, media, media_directory, *, allow_unprotected=False):
    """Raise UnsuitableMediaFolder if the restore would refuse this folder once it had begun."""
    backup, media_directory = Path(os.path.abspath(backup)), Path(os.path.abspath(media_directory))
    if media_directory.exists() or media_directory.is_symlink():
        raise UnsuitableMediaFolder(f'{media_directory} already exists. The restore makes a new library folder: '
                                    'choose a name that is not there yet.')
    parent = media_directory.parent
    if not parent.is_dir():
        raise UnsuitableMediaFolder(f'{parent} is not a folder. Choose an existing folder to hold the library folder.')
    if media_directory.is_relative_to(backup) or backup.is_relative_to(media_directory):
        raise UnsuitableMediaFolder('The library folder must be separate from the backup folder.')
    if media == 'in-place':
        try:
            same = volume_identity(parent) == volume_identity(backup)
        except OSError:
            same = False
        if not same:
            raise UnsuitableMediaFolder(
                f'In place keeps the photos and videos on the backup\'s disk, so the library folder must be on '
                f'that disk too, and {media_directory} is on another disk. Choose a folder on the backup\'s disk, '
                'or copy the photos and videos instead.')
    try:
        check_destination_privacy(parent, allow_unprotected=allow_unprotected)
    except UnprotectedDestination:
        raise UnsuitableMediaFolder(
            f'The disk holding {media_directory} cannot keep files private (exFAT works this way): anyone with '
            'the disk can read the photos and videos. Accept that, or choose a folder on another disk.',
            unprotected=True) from None
    except PermissionError:
        raise UnsuitableMediaFolder(f'FamilyFoto cannot write in {parent}. Choose another folder.') from None
    except OSError as error:
        # A disk that went away, or one that refuses the probe some other way: a question, not a traceback.
        raise UnsuitableMediaFolder(f'FamilyFoto could not check {parent} ({error.strerror or type(error).__name__}). '
                                    'Check the disk is connected, or choose another folder.') from None
