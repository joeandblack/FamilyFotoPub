"""Audit or repair missing photo thumbnails and video posters; originals are never modified."""
import argparse
import json
import os
from app.config import DATA, load_config
from app.database import connection
from app.storage.disks import DiskError, Monitor
from app.jobs import idle
from app.telemetry import utcnow, write_json
from app.photos.thumbnail_recovery import rebuild_missing
from app.video.poster_repair import ready_tools, rebuild_missing_poster, videos_with_posters


def run(apply=False, ids=None):
    config = load_config()
    report = {'started': utcnow(), 'state': 'audit', 'rebuilt': [], 'recovered': [], 'failed': [], 'excluded': [], 'exists': []}
    with connection() as db:
        if apply:idle(db)
        query = "SELECT m.* FROM media m WHERE m.kind='image' AND left(m.name,1)<>'.' AND NOT EXISTS(SELECT 1 FROM media_trash t WHERE t.media_id=m.id AND t.deleting_at IS NOT NULL)"
        rows = db.execute(query + (' AND m.id=ANY(%s)' if ids else '') + ' ORDER BY m.id', (ids,) if ids else ()).fetchall()
        db.commit()
        cached = {name[:-4] for root, dirs, files in os.walk(DATA/'thumbnails') for name in files if name.endswith('.jpg')}
        missing = [r for r in rows if r['sha256'] not in cached]
        clips = videos_with_posters(db, ids)
        db.commit()
        missing_clips = [r for r in clips if r['sha256'] not in cached]
        report.update(photos=len(rows), missing=len(missing), videos=len(clips), missing_posters=len(missing_clips))
        print(json.dumps({'photos': len(rows), 'missing': len(missing), 'videos': len(clips),
                          'missing_posters': len(missing_clips)}), flush=True)
        if not apply:return report
        tools = None
        if missing_clips:
            try:
                tools = ready_tools()
            except (FileNotFoundError, RuntimeError) as error:
                # Photos still repair; the posters wait for the decoders to be set up.
                report['failed'].append({'kind': 'video', 'videos': len(missing_clips), 'error': str(error)[:500]})
                missing_clips = []
        path = DATA/('thumbnail-repair-' + utcnow().replace(':','-') + '.json')
        report['state'] = 'running'
        monitor = Monitor(config, roles=('destination',))
        if not monitor.marker.exists():raise RuntimeError('Existing destination marker required')
        monitor.prepare()
        try:
            for kind, row in [('photo', r) for r in missing] + [('video', r) for r in missing_clips]:
                if db.execute("SELECT 1 FROM upload_sessions WHERE state IN ('uploading','queued','importing','cancelling') LIMIT 1").fetchone():
                    report['state'] = 'deferred-for-upload'
                    break
                db.commit()
                try:
                    if kind == 'photo':
                        outcome = rebuild_missing(db, row, config, DATA, monitor.check)
                    else:
                        outcome = rebuild_missing_poster(db, row, config, tools, monitor.check)
                    db.commit()
                    report[outcome].append(row['id'])
                except Exception as error:
                    db.rollback()
                    report['failed'].append({'id': row['id'], 'name': row['name'], 'error': str(error)[:500]})
                    if kind == 'video' and isinstance(error, DiskError):
                        # Storage or a decoder needs review, as in the video job: stop here.
                        report['state'] = 'paused-error'
                        write_json(path, report)
                        break
                report['updated'] = utcnow()
                write_json(path, report)
                print(json.dumps({'id': row['id'], 'kind': kind, **{k: len(report[k]) for k in ('rebuilt','recovered','failed')}}), flush=True)
            else:report['state'] = 'completed'
        finally:
            monitor.close()
            report['finished'] = utcnow()
            write_json(path, report)
    print(json.dumps({'state': report['state'], 'report': str(path), **{k: len(report[k]) for k in ('rebuilt','recovered','failed')}}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--ids', nargs='+', type=int)
    args = parser.parse_args()
    run(args.apply, args.ids)
