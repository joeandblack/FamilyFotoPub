"""Hashed credentials and durable, individually revocable browser sessions."""
import hashlib
import hmac
import json
import re
import secrets
import time

from .store import SessionStore

ROLES = {'admin', 'regular', 'guest'}
PASSWORD_ROUNDS = 600000
SESSION_SECONDS = 43200
HASH_PATTERN = re.compile(r'pbkdf2_sha256\$600000\$[0-9a-f]{32}\$[0-9a-f]{64}')
# Unknown accounts perform the same password work, without revealing existence.
DUMMY_HASH = 'pbkdf2_sha256$600000$' + '00' * 16 + '$' + '00' * 32


def password_hash(password):
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise ValueError('Password must contain between 1 and 1024 characters')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), PASSWORD_ROUNDS).hex()
    return f'pbkdf2_sha256${PASSWORD_ROUNDS}${salt}${digest}'


def signing_key(config):
    secret = config.get('auth_secret')
    if not isinstance(secret, str) or len(secret) < 43:
        raise ValueError('Authentication settings need upgrading; run scripts/upgrade_auth.py')
    return secret.encode()


def accounts(config):
    if 'browser_password' in config:
        raise ValueError('Plaintext browser passwords are no longer accepted; run scripts/upgrade_auth.py')
    result = dict(config.get('browser_accounts', {}))
    if config['browser_user'] in result:
        raise ValueError('Administrator username cannot also appear in browser_accounts')
    result[config['browser_user']] = {'role': 'admin', 'password_hash': config.get('browser_password_hash')}
    for name, account in result.items():
        if (not isinstance(name, str) or not 1 <= len(name) <= 128 or
                not isinstance(account, dict) or account.get('role') not in ROLES or
                'password' in account or not HASH_PATTERN.fullmatch(str(account.get('password_hash', '')))):
            raise ValueError('Invalid account settings; only salted password hashes are accepted')
    signing_key(config)
    return result


def verify_password(account,password):
    encoded = account.get('password_hash', '')
    if (not isinstance(password, str) or not 1 <= len(password) <= 1024 or
            not isinstance(encoded, str) or not HASH_PATTERN.fullmatch(encoded)):
        return False
    _, rounds, salt, expected = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return secrets.compare_digest(actual, expected)


def credential_signature(config, username, account):
    payload = json.dumps([username, account['role'], account['password_hash'], config.get('session_epoch')])
    return hmac.new(signing_key(config), payload.encode(), hashlib.sha256).hexdigest()


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session_token(config,expires,username=None):
    username = username or config['browser_user']
    account = accounts(config)[username]
    token = secrets.token_urlsafe(32)
    SessionStore(config).create(token_digest(token), username,
                                credential_signature(config, username, account),
                                min(int(expires), int(time.time()) + SESSION_SECONDS))
    return token


def session_user(config,token):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        return None
    row = SessionStore(config).get(token_digest(token))
    if not row:
        return None
    account = accounts(config).get(row['username'])
    if account and secrets.compare_digest(row['credential_signature'],
                                          credential_signature(config, row['username'], account)):
        return {'username': row['username'], 'role': account['role']}
    return None


def valid_session(config,token):
    return session_user(config,token) is not None


def revoke_session(config, token):
    if isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        SessionStore(config).revoke(token_digest(token))


def revoke_sessions(config, username):
    return SessionStore(config).revoke_user(username)


def reserve_password_attempt(config, username, address):
    """Share password retry budgets between sign-in and sensitive account changes."""
    def bucket(kind, value):
        return kind + ':' + hmac.new(signing_key(config), value.encode(), hashlib.sha256).hexdigest()
    account_key = bucket('account', username)
    return SessionStore(config).admit_login(account_key, bucket('ip', address))


def authenticate(config, username, password, address):
    known_accounts = accounts(config)
    retry = reserve_password_attempt(config, username, address)
    if retry:
        return None, retry
    account = known_accounts.get(username)
    valid = verify_password(account or {'password_hash': DUMMY_HASH}, password)
    if not valid or account is None:
        return None, 0
    return session_token(config, int(time.time()) + SESSION_SECONDS, username), 0
