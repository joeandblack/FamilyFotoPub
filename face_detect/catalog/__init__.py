"""The single People catalog and where it lives.

An installation has one People catalog, the schema `people`, beside the main schema `public`.
People code runs with the search path `people, public`: its own tables come from the
catalog, and `media`, `media_trash` and the other application tables from the main schema.
Tests use their own pair of schemas, which is why both names are parameters.
"""
import copy
from pathlib import Path
import re

from psycopg import sql

SCHEMA = 'people'
MAIN = 'public'
DDL = (Path(__file__).resolve().parents[1] / 'storage/catalog/001_people.sql')
# Schemas that held People data before the one catalog. Photo deletion still cleans them,
# and the migration reads from the first kind.
OLDER_SCHEMA = re.compile(r'(?:face_native|auto_eval)_[0-9a-f]{32}')
NAME = re.compile(r'[a-z_][a-z0-9_]{0,62}')


def search_path(schema=SCHEMA, main=MAIN):
    for name in (schema, main):
        if not NAME.fullmatch(name):
            raise ValueError('Invalid schema name')
    return f'{schema}, {main}'


def database(cfg, schema=SCHEMA, main=MAIN):
    """Connection settings for People code: the installation's database, with the catalog first."""
    value = copy.deepcopy(cfg['database'])
    value['options'] = (value.get('options', '') + ' -c search_path=' + search_path(schema, main).replace(' ', '')).strip()
    return value


def exists(db, schema=SCHEMA):
    return bool(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone())


def create(db, schema=SCHEMA, main=MAIN):
    """Create the catalog in the caller's transaction. The search path comes back afterwards."""
    previous = _value(db.execute("SELECT current_setting('search_path') p").fetchone())
    db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    db.execute("SELECT set_config('search_path', %s, true)", (search_path(schema, main),))
    # Unqualified names in the DDL must land in the catalog, never in the main schema.
    if _value(db.execute('SELECT current_schema() s').fetchone()) != schema:
        raise RuntimeError('The People catalog schema is not first on the search path')
    db.execute(DDL.read_text(encoding='utf-8'))
    db.execute("SELECT set_config('search_path', %s, true)", (previous,))


def link_pilot(db):
    """The name collection links use for this catalog: its UUID, written as 32 hex digits.
    It goes with the library, so a link made on Windows still works on a Mac."""
    row = db.execute('SELECT catalog_uuid FROM catalog_state WHERE singleton').fetchone()
    return _value(row).hex if row else None


def people_schemas(db):
    """Every schema that holds People data: the catalog and any older per-model schema.
    Shared by photo deletion and the catalog code, so a schema move can't skip either."""
    rows = db.execute("SELECT nspname FROM pg_namespace WHERE nspname = %s "
                      "OR nspname ~ '^(face_native|auto_eval)_[0-9a-f]{32}$' ORDER BY nspname <> %s, nspname",
                      (SCHEMA, SCHEMA)).fetchall()
    return [name for name in map(_value, rows) if name == SCHEMA or OLDER_SCHEMA.fullmatch(name)]


def _value(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]
