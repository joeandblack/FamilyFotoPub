"""A computer writes vectors into the library's space only once it has passed the library check.

The check (face_detect/spaces/library_check.py) compares this computer's vectors with the
stored ones on a sample of the library's own faces. Its certificate is stored in the
catalog, keyed by space and backend, so it runs once per computer and model setup: the
first Discover people on a new computer runs it, read-only, and then scans. The matching
policy applies to this computer from then on (docs/PEOPLE.md, "Moving to another computer").

A library with no stored vectors in the space has nothing to compare with and nothing to
adopt, so it needs no check. The `origin` record of the computer that made the vectors is
never a passed check.
"""
import json

from face_detect.spaces.identity import backend, backend_id

# One worker at a time runs the check for this library; the others wait for its result.
CHECK_LOCK = 4340061
# A failed check is not repeated by every worker start; it's shown again until this passes.
RETRY_SECONDS = 600


class NotCertified(ValueError):
    pass


def certificate(db, space, backend_value):
    return db.execute("SELECT id, checked_at FROM certificate WHERE space_id=%s AND backend_id=%s "
                      "AND reference<>'origin' AND passed ORDER BY id DESC LIMIT 1",
                      (space, backend_id(backend_value))).fetchone()


def may_write(db, space, backend_value):
    """Whether vectors from this backend may enter the space: it passed the library check, or
    the space holds no vectors yet, so there is nothing to compare with or adopt."""
    if certificate(db, space, backend_value):
        return True
    return not db.execute('SELECT EXISTS(SELECT 1 FROM embedding WHERE space_id=%s) x', (space,)).fetchone()['x']


def needs_check(db, provider):
    """None if this computer may write; otherwise why it must pass the check first."""
    if may_write(db, provider.space_id, backend(provider.manifest)):
        return None
    return 'This computer has not been checked against the library yet'


def recent_failure(db, space, backend_value, seconds=None):
    seconds = RETRY_SECONDS if seconds is None else seconds
    return db.execute("SELECT result FROM certificate WHERE space_id=%s AND backend_id=%s AND NOT passed "
                      "AND reference<>'origin' AND checked_at > now() - make_interval(secs => %s) "
                      'ORDER BY id DESC LIMIT 1', (space, backend_id(backend_value), seconds)).fetchone()


def record(db, result):
    """Store a library-check certificate. Only a passed one lets the backend write."""
    db.execute("INSERT INTO certificate(space_id, backend, backend_id, reference, passed, result) "
               "VALUES (%s, %s, %s, %s, %s, %s)",
               (result['space_id'], json.dumps(result['backend']), result['backend_id'],
                result.get('reference', 'library-sample'), bool(result['passed']),
                json.dumps({k: v for k, v in result.items() if k not in ('backend',)}, default=str)))


def ensure_certified(connect, provider, check):
    """Run `check()` (the library check, returning its certificate) if this computer needs one,
    record it, and refuse to go on unless it passed."""
    with connect() as db:
        reason = needs_check(db, provider)
    if reason is None:
        return None
    # Workers starting together run the check once: the others wait, then see its result.
    with connect() as lock:
        lock.execute('SELECT pg_advisory_lock(%s)', (CHECK_LOCK,))
        try:
            with connect() as db:
                if needs_check(db, provider) is None:
                    return None
                failed = recent_failure(db, provider.space_id, backend(provider.manifest))
            if failed:
                raise NotCertified('This computer gives different face vectors from the ones in the library '
                                   f"({failed['result'].get('reason')}); nothing was written")
            result = check()
            with connect() as db:
                record(db, result)
        finally:
            lock.execute('SELECT pg_advisory_unlock(%s)', (CHECK_LOCK,))
    if not result.get('passed'):
        raise NotCertified('This computer gives different face vectors from the ones in the library '
                           f"({result.get('reason')}); nothing was written")
    return result


def write_blocker(cfg):
    """None when this library has its People catalog and this computer may write to it: its
    face model is the catalog's active space, and the library check has passed (or the library
    has no vectors to check). Otherwise the reason, in a sentence. Never raises, so a caller
    can fall back to viewing and say why."""
    import psycopg
    from psycopg.rows import dict_row
    from face_detect import catalog
    from face_detect.catalog.spaces import active_space
    from face_detect.service import create_service
    try:
        provider = create_service(cfg)
    except Exception:
        return 'the face engine is not set up on this computer'
    try:
        if provider.space_id is None:
            return 'this computer\'s face model defines no embedding space'
        with psycopg.connect(**catalog.database(cfg), row_factory=dict_row, connect_timeout=5) as db:
            db.execute('SET default_transaction_read_only = on')
            if not catalog.exists(db):
                return 'the library has no People catalog'
            if active_space(db) != provider.space_id:
                return "this computer's face model is not the library's active model"
            return needs_check(db, provider)
    except Exception:
        return 'the People catalog could not be read'
    finally:
        provider.close()


def this_computer_may_write(cfg):
    return write_blocker(cfg) is None
