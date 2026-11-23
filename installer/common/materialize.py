"""Platform-neutral wheelhouse and dependency-lock materialization."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from .acquire import file_digest, plain_path, verified
from .manifest import LOCK_ROLES, safe_relative
from .state import atomic_json


def materialize(manifest, receipt_path, platform, os_version=None):
    receipt_path = plain_path(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    if (receipt.get('schema_version') != 1 or receipt.get('stage') not in ('acquired', 'materialized')
            or receipt.get('manifest_digest') != manifest.digest):
        raise ValueError('Prepared release does not match manifest')
    selected = manifest.plan(receipt['features'], platform, os_version)
    root = receipt_path.parent
    wheels = root / 'wheels'
    locks = root / 'locks'
    plain_path(wheels).mkdir(exist_ok=True)
    plain_path(locks).mkdir(exist_ok=True)
    lock_map = {}
    for item in selected:
        source = plain_path(receipt['paths'][item['id']])
        if not verified(source, item):
            raise ValueError('Prepared component changed')
        filename = safe_relative(item['bundle_path']).name
        role = item.get('lock_role')
        if filename.endswith('.whl'):
            target = plain_path(wheels / filename)
        elif role:
            if role not in LOCK_ROLES:
                raise ValueError('Unknown dependency lock role')
            target = plain_path(locks / (role + '.txt'))
            if role in lock_map:
                raise ValueError('Duplicate dependency lock role')
            lock_map[role] = {'path': str(target), 'sha256': item['sha256']}
        else:
            continue
        if target.exists():
            if file_digest(target) != item['sha256']:
                raise ValueError('Conflicting local payload')
        else:
            # Never leave an interrupted copy at a reusable payload name.
            fd, temporary = tempfile.mkstemp(prefix='.materialize-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as dst, source.open('rb') as src:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                if not verified(Path(temporary), item):
                    raise ValueError('Materialized component changed')
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
    if 'core' not in lock_map:
        raise ValueError('Release is missing the complete core dependency lock')
    receipt.update(stage='materialized', wheelhouse=str(wheels), locks=lock_map)
    atomic_json(receipt_path, receipt)
    return receipt
