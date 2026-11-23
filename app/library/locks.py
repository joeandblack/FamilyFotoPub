"""Per-content session locks survive commits and release on connection close."""

MEDIA_LOCK_NAMESPACE = 4340004


def content_key(sha):
    value = int(sha[:8], 16)
    return value if value < 2**31 else value - 2**32


def lock_content(db, sha):
    key = content_key(sha)
    db.execute('SELECT pg_advisory_lock(%s, %s)', (MEDIA_LOCK_NAMESPACE, key))
    return key


def unlock_content(db, key):
    db.execute('SELECT pg_advisory_unlock(%s, %s)', (MEDIA_LOCK_NAMESPACE, key))
