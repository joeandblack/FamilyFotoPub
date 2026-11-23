"""A throwaway People catalog beside a test's main schema, the way an installation has
`people` beside `public`. Tests that touch People data use it through `catalog_beside`."""
from contextlib import contextmanager
import uuid

from psycopg import sql

from app import database
from face_detect import catalog


@contextmanager
def catalog_beside(config):
    """Create a catalog schema next to the current test schema and put it first on the
    search path of `config`, so People code finds its tables there and photos in the main
    schema. The schema is dropped afterwards."""
    with database.connection() as db:
        main = db.execute('SELECT current_schema() s').fetchone()['s']
    schema = 'peopletest_' + uuid.uuid4().hex
    with database.connection() as db:
        catalog.create(db, schema, main=main)
    original = config['database'].get('options')
    config['database']['options'] = '-c search_path=' + catalog.search_path(schema, main).replace(' ', '')
    try:
        yield schema, main
    finally:
        config['database']['options'] = original
        with database.connection() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def step_aside_active_space():
    """Let a test's own scanner become the library's model: the space active so far stays,
    no longer active. Without this a second model is refused, as in an installation."""
    with database.connection() as db:
        db.execute("UPDATE space SET state='building' WHERE state='active'")
        db.execute('DELETE FROM catalog_state')
