"""Live import selection for the separate, durable recognition catalog.

Media IDs stay identical to the library; recognition IDs never mix with the old
pilot. Enrollment copies catalog references, never source media or face results.
"""
import re
import threading
import time
from app.database import connection as database_connection
from app.jobs.job_access import upload_clause, visible_job
from app.viewer import current as current_viewer, media_visible


class ImportCatalog:
    def __init__(self, config, connection):
        self.config, self.connection = config, connection
        self._selection_lock=threading.Lock()
        self._selection_cache={}  # per viewer: what is visible depends on who is asking

    def live(self):
        return database_connection(self.config['database'])

    def selections(self,cache_seconds=0):
        with self._selection_lock:
            who=current_viewer()
            cached=self._selection_cache.get(who)
            if cached is not None and time.monotonic()-cached[0]<cache_seconds:
                return cached[1]
            result=self._selections()
            self._selection_cache[who]=(time.monotonic(),result)
            return result

    def job_name_visible(self,name):
        """Whether the viewer may read progress of People job `name` (import-<id>[-photo-<n>])."""
        match=re.match(r'import-([1-9][0-9]*)(?:-|$)',name)
        if match is None or current_viewer() is None:return True
        return int(match[1]) in self.visible_job_ids()

    def visible_job_ids(self):
        """Import/upload job ids the current viewer may see progress for."""
        hide,values=upload_clause()
        with self.live() as db:
            db.execute('SET TRANSACTION READ ONLY')
            return {r['id'] for r in db.execute('SELECT j.id FROM jobs j LEFT JOIN upload_sessions u ON u.job_id=j.id'+(' WHERE '+hide if hide else ''),values).fetchall()}

    def _selections(self):
        with self.live() as db:
            db.execute('SET TRANSACTION READ ONLY')
            hide,hide_values=upload_clause()
            rows = db.execute("""WITH eligible AS MATERIALIZED (
                SELECT m.id,m.first_import_job_id FROM media m WHERE m.kind='image'
                AND left(m.name,1)<>'.' AND """+media_visible('m')+"""
                AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)
                AND NOT EXISTS(SELECT 1 FROM face_preferences p WHERE p.media_id=m.id AND p.opted_out)),
                links AS (
                SELECT first_import_job_id job_id,id media_id FROM eligible WHERE first_import_job_id IS NOT NULL
                UNION SELECT i.job_id,i.media_id FROM import_items i JOIN eligible e ON e.id=i.media_id)
                SELECT j.id,count(*) n FROM jobs j JOIN links l ON l.job_id=j.id
                LEFT JOIN upload_sessions u ON u.job_id=j.id
                WHERE j.state='completed-review-required'"""+(' AND '+hide if hide else '')+"""
                GROUP BY j.id ORDER BY j.id""",hide_values).fetchall()
        return [{'id':f"import:{r['id']}", 'total':r['n'], 'label':f"Import #{r['id']} · {r['n']} photos"} for r in rows]

    def resolve(self, selection):
        if not re.fullmatch(r'import:[1-9][0-9]*', selection):
            raise ValueError('Choose a completed import')
        jid = int(selection.split(':')[1])
        with self.live() as live:
            live.execute('SET TRANSACTION READ ONLY')
            job = live.execute('SELECT state FROM jobs WHERE id=%s', (jid,)).fetchone()
            if job and not visible_job(live, jid):
                job = None  # someone else's upload batch looks like a job that does not exist
            if not job or job['state']!='completed-review-required':
                raise ValueError('Finish the photo/video import before discovering people')
            rows = live.execute("""SELECT m.id,m.sha256,m.relative_path,m.name,m.kind,m.size,
                m.captured_at,m.width,m.height FROM media m WHERE m.kind='image'
                AND left(m.name,1)<>'.' AND """+media_visible('m')+"""
                AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id)
                AND NOT EXISTS(SELECT 1 FROM face_preferences p WHERE p.media_id=m.id AND p.opted_out)
                AND (m.first_import_job_id=%s OR EXISTS
                    (SELECT 1 FROM import_items i WHERE i.job_id=%s AND i.media_id=m.id))
                ORDER BY m.id""", (jid,jid)).fetchall()
        if not rows:
            raise ValueError('No eligible photos in this import')
        # The People catalog reads the main photo list directly, so there is nothing to copy:
        # the photos, their import and its items are already where People looks for them.
        return [r['id'] for r in rows]
