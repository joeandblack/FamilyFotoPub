"""Explicit, atomic upgrade of private browser credentials. Stop HTTP services first."""
import copy
import ctypes
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.auth import accounts, password_hash, verify_password
from app.backup.admission import guard_application_path
from app.config import configuration_root


def upgraded_config(config):
    updated = copy.deepcopy(config)
    if 'browser_password' in updated:
        password = updated.pop('browser_password')
        existing = updated.get('browser_password_hash')
        if existing and not verify_password({'password_hash': existing}, password):
            raise ValueError('Conflicting administrator credentials; resolve before upgrading')
        updated['browser_password_hash'] = existing or password_hash(password)
    for account in updated.get('browser_accounts', {}).values():
        if 'password' in account:
            password = account.pop('password')
            existing = account.get('password_hash')
            if existing and not verify_password({'password_hash': existing}, password):
                raise ValueError('Conflicting account credentials; resolve before upgrading')
            account['password_hash'] = existing or password_hash(password)
    if 'auth_secret' not in updated:
        updated['auth_secret'] = secrets.token_urlsafe(32)
    accounts(updated)
    return updated


def upgrade(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Configuration must not be a symbolic link')
    path = path.resolve()
    guard_application_path(path)
    original = path.read_bytes()
    config = json.loads(original)
    updated = upgraded_config(config)
    if updated == config:
        return False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.auth-', delete=False) as file:
            temporary = Path(file.name)
            json.dump(updated, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        if path.read_bytes() != original:
            raise RuntimeError('Configuration changed; retry with HTTP services stopped')
        if os.name == 'nt':
            # ReplaceFile preserves the existing file's DACL; no plaintext backup.
            replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
            replace.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                               ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
            replace.restype = ctypes.c_int
            if not replace(str(path), str(temporary), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return True


if __name__ == '__main__':
    changed = upgrade(configuration_root()/'local.json')
    print('Browser credentials upgraded; passwords unchanged. Restart both HTTP services.'
          if changed else 'Browser credentials are already upgraded.')
