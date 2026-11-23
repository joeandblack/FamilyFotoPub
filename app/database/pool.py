"""Bounded process-local pools with connection-close-equivalent session cleanup."""
import atexit
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import threading
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from .. import viewer

MAX_CONFIGURATIONS = 4
MAX_CONNECTIONS = 8
CHECKOUT_TIMEOUT = 5
_lock = threading.Lock()
_pools = OrderedDict()
log = logging.getLogger(__name__)


@dataclass
class Entry:
    pool: ConnectionPool
    users: int = 0


@contextmanager
def _pool(settings):
    # Credentials and search_path both participate, but are not registry labels.
    key = (os.getpid(), hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest())
    with _lock:
        entry = _pools.get(key)
        if entry is None:
            if len(_pools) >= MAX_CONFIGURATIONS:
                idle = next((k for k, v in _pools.items() if not v.users), None)
                if idle is None:
                    raise RuntimeError('Too many active database configurations')
                _pools.pop(idle).pool.close()
            pool = ConnectionPool(
                kwargs={**settings, 'row_factory': dict_row, 'connect_timeout': 5,
                        'prepare_threshold': None, 'autocommit': False},
                min_size=0, max_size=MAX_CONNECTIONS, max_idle=60,
                timeout=CHECKOUT_TIMEOUT, max_waiting=64,
                check=ConnectionPool.check_connection, open=False,
                name='familyfoto-db')
            pool.open()
            entry = _pools[key] = Entry(pool)
        _pools.move_to_end(key)
        entry.users += 1
    try:
        yield entry.pool
    finally:
        with _lock:
            entry.users -= 1


def _reset(db):
    if db.closed:
        return
    if db.info.transaction_status != TransactionStatus.IDLE:
        db.rollback()
    # Reset synchronously before returning: releasing locks in a pool background
    # callback would extend worker ownership beyond the connection context.
    db.autocommit = True
    db.execute('DISCARD ALL')
    db.isolation_level = None
    db.read_only = None
    db.deferrable = None
    db.row_factory = dict_row
    db.prepare_threshold = None
    db.autocommit = False


@contextmanager
def pooled_connection(settings):
    with _pool(dict(settings)) as pool:
        db = pool.getconn()
        try:
            viewer.apply(db)
            with db:
                yield db
        finally:
            try:
                _reset(db)
            except Exception:
                # Never reuse a session whose locks/state could not be cleared.
                log.warning('Discarding database connection after failed session reset')
                db.close()
            finally:
                pool.putconn(db)


def close_pools():
    with _lock:
        pools = [entry.pool for entry in _pools.values()]
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)
