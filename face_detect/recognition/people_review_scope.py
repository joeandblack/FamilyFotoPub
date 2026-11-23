"""Import membership for review; matching reference people remain global."""

LIVE_VISIBILITY = ''  # Installed by the isolated runtime, using its verified live schema.


def scanned_imports(db):
    from face_detect.recognition.faces import VISIBLE
    rows=db.execute('''WITH scanned AS (
        SELECT DISTINCT m.id,m.first_import_job_id FROM media m JOIN face_analyses a ON a.media_id=m.id
        WHERE a.outcome='done' AND a.content_hash=m.sha256 AND m.kind='image' AND '''+VISIBLE+LIVE_VISIBILITY+'''),
        links AS (
        SELECT first_import_job_id job_id,id media_id FROM scanned WHERE first_import_job_id IS NOT NULL
        UNION SELECT i.job_id,i.media_id FROM import_items i JOIN scanned s ON s.id=i.media_id)
        SELECT job_id,count(*) photos FROM links GROUP BY job_id ORDER BY job_id''').fetchall()
    return [{'id':f"import:{r['job_id']}", 'total':r['photos'],
             'label':f"Import #{r['job_id']} - {r['photos']:,} scanned {'photo' if r['photos']==1 else 'photos'}"} for r in rows]


def media_ids(db, import_id):
    if import_id is None:
        return None
    if type(import_id) is not int or import_id <= 0:
        raise ValueError('Choose a valid import')
    return {r['id'] for r in db.execute('''SELECT m.id FROM media m
        WHERE m.kind='image' AND (m.first_import_job_id=%s OR EXISTS
        (SELECT 1 FROM import_items i WHERE i.media_id=m.id AND i.job_id=%s))''',
        (import_id, import_id)).fetchall()}


def require_faces(db, import_id, ids):
    if import_id is None:
        return
    scope = media_ids(db, import_id)
    found = db.execute('SELECT d.id,a.media_id FROM face_detections d JOIN face_analyses a '
                       'ON a.id=d.analysis_id WHERE d.id=ANY(%s)', (list(ids),)).fetchall()
    if len(found) != len(set(ids)) or any(r['media_id'] not in scope for r in found):
        raise ValueError('Selected faces do not belong to this import')
