"""A sealed receipt of one full read of a media tree, so later steps need not repeat it.

Reading 3 TB takes hours, and one recovery used to do it for every step. A receipt records
that the tree matched its inventory, file by file, and what every file looked like then.
A later step walks the tree, which takes minutes, and accepts the receipt only when the
same disk holds the same files with the same sizes and modification times.

The receipt is sealed with the recovery key and kept in the recovery folder, never beside
the media. It detects files added, removed, resized or rewritten since the full read. It
does not detect a disk that silently changed bytes without touching a file's size or time;
`reverify` forces the full read again for that. Anything wrong with a receipt means a full
read, never a refusal.
"""
from datetime import datetime, timezone
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .encrypted import read_bounded
from .media_tree import verify_media_tree
from .recovery import save_private
from .space import directory_identity
from .volumes import same_except_device, volume_identity


FORMAT = 'familyfoto-media-verified-v1'
CANDIDATE = 'MEDIA-VERIFIED.json'
BACKUP = 'BACKUP-MEDIA-VERIFIED.json'


def _binding(tree, inventory_sha256):
    tree = Path(tree)
    return {'format': FORMAT, 'tree': str(tree), 'identity': list(directory_identity(tree / 'media')),
            'volume': volume_identity(tree), 'inventory_sha256': inventory_sha256}


def write_receipt(directory, key, tree, media, *, name=CANDIDATE):
    """Record a full read that has just succeeded. `media` is verify_media_tree's result."""
    if media.get('content_verified') is not True:
        raise ValueError('Only a full read of the media earns a receipt')
    record = {**_binding(tree, media['inventory_sha256']), 'files': media['files'], 'bytes': media['bytes'],
              'tree_signature': media['tree_signature'], 'verified_at': datetime.now(timezone.utc).isoformat()}
    save_private(Path(directory) / name, {'sealed': Fernet(key).encrypt(json.dumps(record).encode()).decode()})
    return record


def read_receipt(directory, key, *, name=CANDIDATE):
    """The receipt, or None when there is none or it can't be trusted."""
    path = Path(directory) / name
    try:
        if path.is_symlink() or not path.is_file():
            return None
        record = json.loads(Fernet(key).decrypt(json.loads(read_bounded(path, 65536))['sealed'].encode()))
    except (OSError, InvalidToken, KeyError, TypeError, AttributeError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get('format') == FORMAT else None


def verified_media(directory, key, tree, *, progress=None, reverify=False, name=CANDIDATE):
    """verify_media_tree's result for `tree`, from a full read or from a receipt of one.

    The quick path still checks the tree's manifests and every name and size against the
    inventory, and fails as the full read would. Only the content read is skipped, and only
    when the receipt names this disk, this folder, this inventory and these exact files.
    """
    receipt = None if reverify else read_receipt(directory, key, name=name)
    if receipt is not None:
        quick = verify_media_tree(tree, content=False)
        expected = _binding(tree, quick['inventory_sha256'])
        same = (all(receipt.get(field) == expected[field] for field in ('tree', 'volume', 'inventory_sha256'))
                and same_except_device(receipt.get('identity'), expected['identity'])
                and (receipt.get('files'), receipt.get('bytes')) == (quick['files'], quick['bytes'])
                and receipt.get('tree_signature') == quick['tree_signature'])
        if same:
            if progress:
                progress({'phase': 'verify-receipt', 'files': quick['files'], 'bytes': quick['bytes'],
                          'verified_at': receipt.get('verified_at')})
            return {**quick, 'content_verified': True, 'from_receipt': True}
    media = verify_media_tree(tree, progress)
    write_receipt(directory, key, tree, media, name=name)
    return media
