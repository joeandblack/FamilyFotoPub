"""Authentication state in the primary catalog, shared by both HTTP services."""
import math

from ..database import connection
from ..database.locks import LOGIN_ADMISSION


class SessionStore:
    def __init__(self, config):
        # People overrides database.load_config with its recognition schema.
        # Authentication must always use the explicit primary catalog settings.
        self.settings = config['database']

    def create(self, digest, username, signature, expires):
        with connection(self.settings) as db:
            db.execute('DELETE FROM auth_sessions WHERE expires_at <= CURRENT_TIMESTAMP')
            db.execute('INSERT INTO auth_sessions(token_hash,username,credential_signature,expires_at) '
                       'VALUES (%s,%s,%s,to_timestamp(%s))', (digest, username, signature, expires))

    def get(self, digest):
        with connection(self.settings) as db:
            return db.execute('SELECT username,credential_signature FROM auth_sessions '
                              'WHERE token_hash=%s AND expires_at>CURRENT_TIMESTAMP', (digest,)).fetchone()

    def revoke(self, digest):
        with connection(self.settings) as db:
            db.execute('DELETE FROM auth_sessions WHERE token_hash=%s', (digest,))

    def activity(self, digest):
        # Preserve the initial guarantee; never revive an expired/revoked token.
        with connection(self.settings) as db:
            return db.execute(
                "UPDATE auth_sessions SET expires_at=GREATEST(expires_at, "
                "CURRENT_TIMESTAMP+interval '1 hour') "
                'WHERE token_hash=%s AND expires_at>CURRENT_TIMESTAMP '
                'RETURNING extract(epoch FROM expires_at-CURRENT_TIMESTAMP)::int AS remaining',
                (digest,)).fetchone()

    def revoke_user(self, username):
        with connection(self.settings) as db:
            return db.execute('DELETE FROM auth_sessions WHERE username=%s', (username,)).rowcount

    def admit_login(self, account_key, address_key):
        # Reserve before hashing, including concurrent attempts. Rejected retries
        # cannot extend the fixed one-minute window or create new bucket rows.
        limits = [('global', 60), (address_key, 20), (account_key, 5)]
        with connection(self.settings) as db:
            db.execute("SET LOCAL lock_timeout='2s'")
            db.execute('SELECT pg_advisory_xact_lock(%s)', (LOGIN_ADMISSION,))
            db.execute('DELETE FROM auth_login_limits WHERE resets_at<=clock_timestamp()')
            rows = {row['bucket']: row for row in db.execute(
                'SELECT bucket,attempts,extract(epoch FROM resets_at-clock_timestamp()) remaining '
                'FROM auth_login_limits WHERE bucket=ANY(%s)', ([key for key, _ in limits],)).fetchall()}
            waits = [float(rows[key]['remaining']) for key, limit in limits
                     if key in rows and rows[key]['attempts'] >= limit]
            if waits:
                return max(1, math.ceil(max(waits)))
            for key, _ in limits:
                db.execute("INSERT INTO auth_login_limits(bucket,attempts,resets_at) "
                           "VALUES (%s,1,clock_timestamp()+interval '1 minute') "
                           'ON CONFLICT(bucket) DO UPDATE SET attempts=auth_login_limits.attempts+1', (key,))
        return 0
