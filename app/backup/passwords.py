"""Backup passwords: the one place a backup's key comes from.

A backup is unlocked by the FamilyFoto administrator password as it was when the backup was
made, not by a key file (docs/BACKUP.md, "Backup password"). One fixed scrypt
profile turns the password and the backup's own random salt into the Fernet key that the
authenticated artifact writer and reader already use; nothing below this module changes.

Each recovery set has a small public `password.json` at its root:

    {"format": "familyfoto-password-v1", "backup_id": "<32 hex>",
     "salt": "<16 bytes, base64url, no padding>", "check": "<Fernet token>"}

`check` encrypts a fixed text naming the format, backup and salt, so a password can be
checked before anything is read or written, even while a capture is unfinished. It is only
a fast unlock: every manifest, chunk and media check still runs, and the sealed library and
catalog indexes repeat `format`, `backup_id` and `salt` (`binding()`), so a replaced
`password.json` can't pass for this backup's own.

Passwords and keys live only in memory. They are never written, logged, put in an argument,
an environment variable or a record. `BackupPassword` and `BackupKey` print as redacted and
refuse to be pickled. Python can't promise the memory is wiped afterwards; we don't claim it.

A password can be guessed offline by anyone holding the backup; scrypt makes each guess cost
about 128 MiB and a fraction of a second. There is no reset: a forgotten password loses the
catalog, although the plain photos and videos on the backup disk stay readable.
"""
import base64
import binascii
import getpass
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import unicodedata
import uuid

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .diagnostics import ExplainedFailure
from .media_copy import sync_directory


FORMAT = 'familyfoto-password-v1'
FILE = 'password.json'
PART = 'password.part'
FILE_LIMIT = 4096
# The one profile of this format. Never read from a disk, never tuned per machine.
SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_BYTES, SALT_BYTES = 2 ** 17, 8, 1, 32, 16
MINIMUM_CHARACTERS = 8
MAXIMUM_BYTES = 1024
# A private pipe carries one frame: 4-byte big-endian length, then this much UTF-8 JSON at most.
FRAME_LIMIT = 8192
CHECK_PURPOSE = 'familyfoto-backup-password-check'
FIELDS = ('format', 'backup_id', 'salt', 'check')

PROTECTS = ("Your FamilyFoto administrator password protects this backup's catalog and application data. "
            'Photos and videos on this backup disk can still be read without it. You will need this same '
            'password to restore the backup, even if you change it later. FamilyFoto cannot reset it.')


class PasswordProblem(ExplainedFailure):
    """A password or password file we can explain, in words with no secret in them."""
    reason = 'password'


class UnsuitablePassword(PasswordProblem):
    reason = 'unsuitable-password'


class WrongPassword(PasswordProblem):
    reason = 'wrong-password'

    def __init__(self, message=None):
        super().__init__(message or "The password is incorrect, or the backup's password information is damaged.")


class UnsupportedBackup(PasswordProblem):
    reason = 'unsupported-backup'

    def __init__(self, message=None):
        super().__init__(message or 'This backup format is not supported. Create a new backup with this version.')


class BackupPassword:
    """A normalized password, held in memory only. `new=True` applies the creation rules."""
    __slots__ = ('_encoded',)

    def __init__(self, text, *, new=False):
        self._encoded = encode(text, new=new)

    @property
    def encoded(self):
        return self._encoded

    def __repr__(self):
        return '<BackupPassword redacted>'

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError('A backup password is never serialized')

    def __eq__(self, other):
        return isinstance(other, BackupPassword) and other._encoded == self._encoded

    __hash__ = None


class BackupKey(bytes):
    """A Fernet key unlocked from a password, with the public context it belongs to."""

    def __new__(cls, key, context):
        value = super().__new__(cls, key)
        value.context = dict(context)
        return value

    def __repr__(self):
        return f'<BackupKey for backup {self.context.get("backup_id")} redacted>'

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError('A backup key is never serialized')

    @property
    def backup_id(self):
        return self.context['backup_id']

    def binding(self):
        """What the sealed indexes repeat, so this key's context is bound to them."""
        return {name: self.context[name] for name in ('format', 'backup_id', 'salt')}


def encode(text, *, new=False):
    """NFC, then UTF-8. Spaces are kept; nothing is stripped, folded or cut short."""
    if not isinstance(text, str):
        raise UnsuitablePassword('The backup password must be text.')
    text = unicodedata.normalize('NFC', text)
    if any(character in text for character in '\0\r\n'):
        raise UnsuitablePassword('The backup password cannot contain line breaks or NUL characters.')
    if not text:
        raise UnsuitablePassword('Enter the backup password.')
    encoded = text.encode('utf-8')
    if len(encoded) > MAXIMUM_BYTES:
        raise UnsuitablePassword(f'The backup password can be at most {MAXIMUM_BYTES} bytes long.')
    if new and len(text) < MINIMUM_CHARACTERS:
        raise UnsuitablePassword(f'The backup password needs at least {MINIMUM_CHARACTERS} characters. '
                                 'A long passphrase of several words is easiest to remember.')
    return encoded


class NotAdministratorPassword(WrongPassword):
    reason = 'not-administrator-password'

    def __init__(self):
        super().__init__('This is not the FamilyFoto administrator password.')


def administrator_account(config_root):
    """The administrator's username and stored hash, from the source's own settings."""
    from .encrypted import read_bounded
    try:
        config = json.loads(read_bounded(Path(config_root) / 'local.json', 1024 ** 2))
    except (OSError, ValueError):
        raise PasswordProblem('The FamilyFoto settings could not be read to check the administrator password.') from None
    username, encoded = config.get('browser_user'), config.get('browser_password_hash')
    if not isinstance(username, str) or not isinstance(encoded, str):
        raise PasswordProblem('This installation has no administrator password to protect a backup with.')
    return username, encoded


def accepts(encoded, text):
    """Does a stored account hash accept this password, exactly as typed (as sign-in does)?"""
    from ..auth import verify_password
    return isinstance(text, str) and verify_password({'password_hash': encoded}, text)


def administrator_password(config_root, text):
    """The password for a NEW backup: the administrator password, checked against its stored hash.

    The backup password is the FamilyFoto administrator password as it is now, so there is no
    second secret to keep. It must be long enough to protect a backup, even though sign-in
    accepts shorter ones."""
    _, encoded = administrator_account(config_root)
    password = BackupPassword(text)
    if not accepts(encoded, text):
        raise NotAdministratorPassword()
    if len(unicodedata.normalize('NFC', text)) < MINIMUM_CHARACTERS:
        raise UnsuitablePassword('Your administrator password is too short to protect a backup. Change it to at '
                                 f'least {MINIMUM_CHARACTERS} characters in Settings, then back up.')
    return password


def salt_text(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def salt_bytes(text):
    if not isinstance(text, str) or not re.fullmatch('[A-Za-z0-9_-]{22}', text):
        raise UnsupportedBackup()
    try:
        raw = base64.urlsafe_b64decode(text + '==')
    except (binascii.Error, ValueError):
        raise UnsupportedBackup() from None
    if len(raw) != SALT_BYTES or salt_text(raw) != text:
        raise UnsupportedBackup()
    return raw


def derive(password, salt):
    """The Fernet key for this password and salt: scrypt N=2^17, r=8, p=1, 32 bytes, base64url."""
    if not isinstance(password, BackupPassword):
        raise TypeError('derive() takes a BackupPassword')
    try:
        raw = Scrypt(salt=salt, length=KEY_BYTES, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(password.encoded)
    except MemoryError:
        raise PasswordProblem('This computer could not set aside the memory the backup password check needs.') from None
    return base64.urlsafe_b64encode(raw)


def check_text(backup_id, salt):
    return '\n'.join((CHECK_PURPOSE, FORMAT, backup_id, salt)).encode('utf-8')


def new_key(password):
    """A key for a NEW backup: a fresh backup_id and salt, in memory until `publish`."""
    salt = os.urandom(SALT_BYTES)
    backup_id = uuid.uuid4().hex
    key = derive(password, salt)
    context = {'format': FORMAT, 'backup_id': backup_id, 'salt': salt_text(salt),
               'check': Fernet(key).encrypt(check_text(backup_id, salt_text(salt))).decode('ascii')}
    return BackupKey(key, context)


def _no_duplicates(pairs):
    names = [name for name, _ in pairs]
    if len(names) != len(set(names)):
        raise UnsupportedBackup()
    return dict(pairs)


def parse(raw):
    """A bounded, exact parse of password.json's bytes. Runs no KDF."""
    if len(raw) > FILE_LIMIT:
        raise UnsupportedBackup()
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except (UnicodeError, ValueError):
        raise UnsupportedBackup() from None
    if not isinstance(value, dict) or set(value) != set(FIELDS) or value['format'] != FORMAT:
        raise UnsupportedBackup()
    if not isinstance(value['backup_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['backup_id']):
        raise UnsupportedBackup()
    salt_bytes(value['salt'])
    if (not isinstance(value['check'], str) or len(value['check']) > 1024
            or not re.fullmatch('[A-Za-z0-9_-]+={0,2}', value['check'])):
        raise UnsupportedBackup()
    return value


def read_context(root):
    """The public context at a recovery set's root. Missing, odd or unsafe means unsupported."""
    path = Path(root) / FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise UnsupportedBackup() from None
    except OSError:
        raise PasswordProblem("The backup's password information could not be read.") from None
    if (path.is_symlink() or path.is_junction() or not stat.S_ISREG(info.st_mode)
            or info.st_size > FILE_LIMIT):
        raise UnsupportedBackup()
    try:
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_size, opened.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise PasswordProblem("The backup's password information changed while it was being read.")
            raw = stream.read(FILE_LIMIT + 1)
    except OSError:
        raise PasswordProblem("The backup's password information could not be read.") from None
    return parse(raw)


def has_context(root):
    path = Path(root) / FILE
    return path.exists() or path.is_symlink()


def open_context(context, password):
    """Derive and authenticate. The check must decrypt to exactly this context's text."""
    key = derive(password, salt_bytes(context['salt']))
    try:
        plain = Fernet(key).decrypt(context['check'].encode('ascii'))
    except (InvalidToken, ValueError):
        raise WrongPassword() from None
    if plain != check_text(context['backup_id'], context['salt']):
        raise WrongPassword()
    return BackupKey(key, context)


def unlock_context(root, password):
    """The key for an existing recovery set. Runs the KDF only on a well-formed context."""
    return open_context(read_context(root), password)


def publish(root, key):
    """Write `key`'s context at `root`, durably, before anything sealed is written there.

    A retry must find the same context: an existing different or damaged file stops the
    capture, and is never replaced. Nothing is deleted.
    """
    if not isinstance(key, BackupKey):
        raise TypeError('publish() takes a BackupKey')
    root = Path(root)
    raw = json.dumps({name: key.context[name] for name in FIELDS}, sort_keys=True).encode('ascii')
    if has_context(root):
        if read_context(root) != {name: key.context[name] for name in FIELDS}:
            raise PasswordProblem('This folder already belongs to another backup password. Choose a new folder.')
        return
    temporary = root / PART
    if temporary.exists() or temporary.is_symlink():
        if temporary.is_symlink() or not temporary.is_file() or temporary.stat().st_size > FILE_LIMIT:
            raise PasswordProblem('This folder holds an unexpected password file. Choose a new folder.')
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / FILE)
    sync_directory(root)
    if read_context(root) != {name: key.context[name] for name in FIELDS}:
        raise PasswordProblem('The backup password information failed read-back verification.')


def create_context(root, password):
    """A new backup's key, its context already published at `root`."""
    key = new_key(password)
    publish(root, key)
    return key


def require_context(root, key):
    """The context at `root` is the one `key` was unlocked from (a copied work-folder context)."""
    if read_context(root) != {name: key.context[name] for name in FIELDS}:
        raise WrongPassword("This folder's password information belongs to another backup.")


def validate_context_binding(key, binding):
    """A sealed index's binding must name the exact context its key was unlocked from."""
    if not isinstance(key, BackupKey) or binding != key.binding():
        raise UnsupportedBackup("This backup's password information belongs to another backup.")


# Input. The CLI reads one line on a terminal or from --backup-password-stdin; internal pipes
# between FamilyFoto's own processes carry one length-prefixed frame and are then closed.
# Readers return the text exactly as typed: a new backup checks it against the administrator's
# stored hash as sign-in does, before it is normalized for the KDF.

def checked_text(text):
    encode(text)
    return text


def prompt(label='FamilyFoto administrator password'):
    if not sys.stdin.isatty():
        raise UnsuitablePassword('No terminal to ask for the administrator password; use --backup-password-stdin.')
    return checked_text(getpass.getpass(label + ': '))


def read_line(stream):
    """One UTF-8 line; only its line ending is removed."""
    raw = stream.read(MAXIMUM_BYTES * 4 + 3)
    if len(raw) > MAXIMUM_BYTES * 4 + 2:
        raise UnsuitablePassword(f'The password can be at most {MAXIMUM_BYTES} bytes long.')
    if raw.endswith(b'\r\n'):
        raw = raw[:-2]
    elif raw.endswith(b'\n'):
        raw = raw[:-1]
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise UnsuitablePassword('The password must be UTF-8 text.') from None
    return checked_text(text)


def frame(text):
    """The private-pipe message for one password, given as typed."""
    payload = json.dumps({'password': checked_text(text)}, ensure_ascii=False).encode('utf-8')
    if len(payload) > FRAME_LIMIT:
        raise UnsuitablePassword('The password is too long.')
    return struct.pack('>I', len(payload)) + payload


def _read_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        part = stream.read(count - len(data))
        if not part:
            raise UnsuitablePassword('The password message was cut short.')
        data.extend(part)
    return bytes(data)


def _frame_pairs(pairs):
    if len({name for name, _ in pairs}) != len(pairs):
        raise UnsuitablePassword('The password message is not valid.')
    return dict(pairs)


def read_frame(stream):
    """Exactly one frame: the length is bounded before anything is allocated."""
    (length,) = struct.unpack('>I', _read_exact(stream, 4))
    if not 2 <= length <= FRAME_LIMIT:
        raise UnsuitablePassword('The password message is not valid.')
    try:
        value = json.loads(_read_exact(stream, length).decode('utf-8'), object_pairs_hook=_frame_pairs)
    except (UnicodeError, ValueError):
        raise UnsuitablePassword('The password message is not valid.') from None
    if not isinstance(value, dict) or set(value) != {'password'} or not isinstance(value['password'], str):
        raise UnsuitablePassword('The password message is not valid.')
    return checked_text(value['password'])


def from_arguments(args):
    """The password text for a CLI command: a framed private pipe, one stdin line, or the terminal."""
    if getattr(args, 'backup_password_frame_stdin', False):
        return read_frame(sys.stdin.buffer)
    if getattr(args, 'backup_password_stdin', False):
        return read_line(sys.stdin.buffer)
    return prompt()


def add_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--backup-password-stdin', action='store_true',
                       help='Read the FamilyFoto administrator password, which protects backups, '
                            'as one UTF-8 line from standard input')
    group.add_argument('--backup-password-frame-stdin', action='store_true', help='Internal: one framed password')
