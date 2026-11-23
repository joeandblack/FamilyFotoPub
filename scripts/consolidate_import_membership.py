"""Inspect legacy import memberships; upgrades use the verified-backup coordinator."""
import argparse
import json
from app.database import connection


def run(apply=False):
    if apply:
        raise RuntimeError('Existing-schema changes require a verified backup. Use installer/upgrade-catalog.py; see docs/BACKUP.md.')
    with connection() as db:
        if db.execute('SELECT 1 FROM schema_versions WHERE version=24').fetchone():
            return {'already_applied':True}
        links=db.execute("SELECT id,job_id,media_id FROM import_items WHERE state='duplicate' AND media_id IS NOT NULL ORDER BY id").fetchall()
        return {'duplicate_links':len(links)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    print(json.dumps(run(parser.parse_args().apply)))
