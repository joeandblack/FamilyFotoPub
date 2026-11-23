"""Common verified media inputs for plain-copy and encrypted-set recoveries."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from ..backup.encrypted import InvalidArtifact
from ..backup.encrypted_library import verify_metadata
from ..backup.library import media_copy_of, verify_library_metadata
from ..backup.library_restore import recovered_media_copy
from ..backup.media_receipt import BACKUP, verified_media
from ..backup.media_tree import file_digest, verify_tree


def candidate_layout(backup, key, directory, progress=None, fresh_restore=False, reverify=False):
    """`fresh_restore` skips rereading media here, for a plain candidate this run just restored
    and whose caller rereads it under the lock. An encrypted candidate has no such reread.

    A plain candidate's media, and its backup's, are read in full once. After that a sealed
    receipt of that read stands for it (app.backup.media_receipt). `reverify` reads again."""
    backup, directory = Path(backup), Path(directory)
    encrypted = (backup / 'recovery-point.json').exists()
    if encrypted:
        from .validation import record
        job = record(directory / 'restore-job.json')
        if backup != directory / 'metadata-job/payload':
            raise InvalidArtifact('Encrypted metadata must be the restored candidate metadata')
        payload = verify_metadata(backup, key, job.get('repository_id'))
        media = directory / 'media-job/payload'
        inventory = backup / 'inventory'
    else:
        source = media_copy_of(backup)
        copy = recovered_media_copy(directory)
        # After an in-place restore the backup's media is this candidate's media, which is
        # reread below. Reading it a second time as the backup would prove nothing more.
        moved_here = Path(os.path.abspath(source)) == Path(os.path.abspath(copy))
        payload = verify_library_metadata(backup, key)
        if not (fresh_restore or moved_here):
            kept = verified_media(directory, key, source, progress=progress, reverify=reverify, name=BACKUP)
            if (kept['files'], kept['bytes']) != (payload.get('media_files'), payload.get('media_bytes')):
                raise InvalidArtifact('Media totals differ from authenticated recovery point')
        media = copy / 'media'
        # The restored inventory has new volume identities. Compare its media
        # against the authenticated source inventory, not that mutable header.
        inventory = source / 'inventory'
        if not fresh_restore:
            # The full read of the candidate, or the receipt of one, against its own inventory
            # copy. The names-and-sizes check below then ties it to the sealed inventory, whose
            # digests equal that copy's: the same file after a move, compared row by row by
            # validation otherwise.
            if not moved_here:
                from ..backup.library_restore import equivalent_inventories
                equivalent_inventories(inventory / 'inventory.sqlite3', copy / 'inventory/inventory.sqlite3')
            verified_media(directory, key, copy, progress=progress, reverify=reverify)
    verify_candidate_media(media, inventory, payload, progress, content=encrypted)
    return payload, media, inventory, encrypted


def verify_candidate_media(media, inventory, payload, progress=None, content=True):
    inventory = Path(inventory) / 'inventory.sqlite3'
    if file_digest(inventory) != payload['media_inventory_sha256']:
        raise InvalidArtifact('Candidate media inventory differs from authenticated recovery point')
    with closing(sqlite3.connect(inventory.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        verify_tree(db, media, content=content, progress=progress)
        count, size = db.execute("SELECT count(*),coalesce(sum(size),0) FROM entries WHERE kind='file'").fetchone()
        if (count, size) != (payload['media_files'], payload['media_bytes']):
            raise InvalidArtifact('Candidate media totals differ')
