"""Administrator-editable settings kept in the database, never in config/local.json."""
from psycopg.types.json import Jsonb


def get(db, key):
    row = db.execute('SELECT value FROM app_settings WHERE key=%s', (key,)).fetchone()
    return row['value'] if row else None


def put(db, key, value, actor):
    db.execute('INSERT INTO app_settings(key,value,updated_by) VALUES(%s,%s,%s) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now(),updated_by=excluded.updated_by',
               (key, Jsonb(value), actor))


def delete(db, key):
    db.execute('DELETE FROM app_settings WHERE key=%s', (key,))
