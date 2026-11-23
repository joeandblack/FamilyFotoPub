import hashlib
import uuid
from test_uploads import env
from app import database
from app.library import trash


def test_original_bulk_filenames_and_soft_delete_permissions(env, tmp_path, monkeypatch):
    client,config,_=env
    ids=[]
    with database.connection() as db:
        for index,kind in enumerate(['image','video','image']):
            raw=f'fixture original {index}'.encode();relative=f'fixture-{index}.jpg'
            (tmp_path/'destination'/relative).write_bytes(raw)
            ids.append(db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES (%s,%s,'same.jpg',%s,%s) RETURNING id",(hashlib.sha256(raw).hexdigest(),relative,kind,len(raw))).fetchone()['id'])
    config['destination'].update(drive='fixture',volume_serial='fixture')
    monkeypatch.setattr('app.storage.disks.volume_serial',lambda _: 'fixture')
    for index,mid in enumerate(ids):
        response=client.get(f'/api/media/{mid}/download?disambiguate=true')
        assert response.content==f'fixture original {index}'.encode()
        assert str(mid) in response.headers['content-disposition']
        assert 'attachment' in response.headers['content-disposition']
    # Regular users may soft-delete photos even during video maintenance.
    client.post('/api/login',json={'username':'reader','password':'fixture'})
    lock=uuid.uuid4().int%(2**63-1);monkeypatch.setattr(trash,'WORKER_LOCK',lock)
    with database.connection() as holder:
        holder.execute('SELECT pg_advisory_lock(%s)',(lock,));holder.commit()
        assert client.post(f'/api/media/{ids[0]}/trash/trash').status_code==200
        assert client.post(f'/api/media/{ids[1]}/trash/trash').status_code==409
    assert client.post(f'/api/media/{ids[0]}/trash/delete').status_code==403
    with database.connection() as db:
        assert [r['media_id'] for r in db.execute('SELECT media_id FROM media_trash').fetchall()]==ids[:1]
    for index in range(3):assert (tmp_path/'destination'/f'fixture-{index}.jpg').read_bytes()==f'fixture original {index}'.encode()
    from app.auth import password_hash
    config['browser_accounts']['guest']={'role':'guest','password_hash':password_hash('fixture')}
    client.post('/api/login',json={'username':'guest','password':'fixture'})
    assert client.post(f'/api/media/{ids[2]}/trash/trash').status_code==403
