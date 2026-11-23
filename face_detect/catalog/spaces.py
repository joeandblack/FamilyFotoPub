"""Spaces and detectors in the People catalog, and the older model IDs that name them.

Vectors are keyed by space and face scans by detector. Records saved before the one catalog
(scan results, undo history, job files) hold the old model ID; the catalog keeps it as
`legacy_model_id`, so those records still resolve.
"""
from psycopg.types.json import Jsonb


class SpaceRefused(ValueError):
    pass


def active_space(db):
    row = db.execute('SELECT active_space_id FROM catalog_state WHERE singleton').fetchone()
    if row:
        return row['active_space_id']
    row = db.execute("SELECT space_id FROM space WHERE state='active'").fetchone()
    return row['space_id'] if row else None


def space_key(db, value):
    """The space a stored ID names: a space ID, or the old model ID it was migrated from."""
    if value is None:
        return None
    row = db.execute('SELECT space_id FROM space WHERE space_id=%s OR legacy_model_id=%s '
                     'ORDER BY space_id=%s DESC LIMIT 1', (value, value, value)).fetchone()
    return row['space_id'] if row else None


def detector_key(db, value):
    if value is None:
        return None
    row = db.execute('SELECT detector_id FROM detector WHERE detector_id=%s OR legacy_model_id=%s '
                     'ORDER BY detector_id=%s DESC LIMIT 1', (value, value, value)).fetchone()
    return row['detector_id'] if row else None


def register_space(db, provider):
    """Record this provider's detector and space before it writes. The first space in an empty
    catalog becomes the active one. A provider of a retired space, or of a second space while
    another is active, is refused: switching spaces is a separate, reviewed step."""
    if not provider.space_id or not provider.detector_id:
        raise SpaceRefused('This face model defines no embedding space')
    db.execute('INSERT INTO detector(detector_id, manifest) VALUES (%s, %s) ON CONFLICT DO NOTHING',
               (provider.detector_id, Jsonb(provider.manifest)))
    current = active_space(db)
    row = db.execute('SELECT state FROM space WHERE space_id=%s', (provider.space_id,)).fetchone()
    if row is None:
        if current is not None:
            raise SpaceRefused('Another face model is active in this library; switching models is a separate step')
        db.execute("INSERT INTO space(space_id, manifest, state) VALUES (%s, %s, 'active')",
                   (provider.space_id, Jsonb(provider.manifest)))
        db.execute('INSERT INTO catalog_state(active_space_id, catalog_uuid) VALUES (%s, gen_random_uuid()) '
                   'ON CONFLICT (singleton) DO NOTHING', (provider.space_id,))
        return provider.space_id
    if row['state'] == 'retired':
        raise SpaceRefused('This face model is retired in this library')
    if current is not None and current != provider.space_id:
        raise SpaceRefused('Another face model is active in this library; switching models is a separate step')
    return provider.space_id
