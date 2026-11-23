"""Backup keys for tests: made from a test administrator password, as a real backup's are.

`conftest.quick_backup_password_kdf` swaps scrypt for a cheap hash outside real_scrypt tests.
"""
import json
from pathlib import Path

from app.auth import password_hash
from app.backup.passwords import BackupPassword, new_key


ADMIN_PASSWORD = 'test administrator password'


def backup_key(password=ADMIN_PASSWORD):
    """A key for a new recovery set, with a fresh context the capture publishes."""
    return new_key(BackupPassword(password))


def set_admin_password(config_root, password=ADMIN_PASSWORD):
    """Give a test source's settings an administrator with this password."""
    path = Path(config_root) / 'local.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    config.setdefault('browser_user', 'family')
    config['browser_password_hash'] = password_hash(password)
    path.write_text(json.dumps(config), encoding='utf-8')
