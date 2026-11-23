"""Administrator account management: add accounts and reset passwords in config/local.json.

Only salted hashes are stored. The file is replaced atomically; on Windows ReplaceFile keeps
the existing file's permissions, so the private settings file never becomes more readable.
"""
import copy
import ctypes
import json
import os
from pathlib import Path
import re
import tempfile

from . import ROLES, accounts, password_hash
from ..backup.admission import guard_application_path
from ..config import configuration_root
from ..config_lock import config_write_lock

USERNAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
MIN_PASSWORD = 8


def config_path():
    return configuration_root() / 'local.json'


def account_list(config):
    primary = config['browser_user']
    return [{'username': name, 'role': account['role'], 'primary': name == primary}
            for name, account in sorted(accounts(config).items(), key=lambda item: item[0].lower())]


def _check_password(password):
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= 1024:
        raise ValueError(f'Password must contain between {MIN_PASSWORD} and 1024 characters')
    if password != password.strip():
        raise ValueError('Password must not start or end with a space')


def with_new_account(config, username, password, role):
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise ValueError('Username must be 1 to 64 letters, digits, dots, dashes or underscores, starting with a letter or digit')
    if role not in ROLES:
        raise ValueError('Choose a role: admin, normal or guest')
    _check_password(password)
    if username.lower() in {name.lower() for name in accounts(config)}:
        raise ValueError('An account with that username already exists')
    updated = copy.deepcopy(config)
    updated.setdefault('browser_accounts', {})[username] = {'role': role, 'password_hash': password_hash(password)}
    accounts(updated)
    return updated


def with_new_password(config, username, password):
    if not isinstance(username, str) or username not in accounts(config):
        raise ValueError('Choose an existing account')
    _check_password(password)
    updated = copy.deepcopy(config)
    if username == updated['browser_user']:
        updated['browser_password_hash'] = password_hash(password)
    else:
        updated['browser_accounts'][username]['password_hash'] = password_hash(password)
    accounts(updated)
    return updated


def _replace(path, temporary):
    if os.name == 'nt':
        replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
        replace.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        replace.restype = ctypes.c_int
        if not replace(str(path), str(temporary), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)


def update_config(change, path=None):
    """Apply `change(config) -> updated` under the shared settings lock.

    The byte check still refuses an edit made outside the lock (a script or a hand edit).
    """
    path = Path(path or config_path())
    if path.is_symlink():
        raise ValueError('Configuration must not be a symbolic link')
    path = path.resolve()
    guard_application_path(path)
    with config_write_lock(path):
        original = path.read_bytes()
        updated = change(json.loads(original))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.accounts-', delete=False) as file:
                temporary = Path(file.name)
                json.dump(updated, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            if path.read_bytes() != original:
                raise RuntimeError('Settings changed while saving; refresh Settings and try again')
            _replace(path, temporary)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return updated
