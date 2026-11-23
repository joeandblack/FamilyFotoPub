from test_albums import catalog, create, members
from test_uploads import env
from app import database


def test_saved_collection_cover_survives_source_removal(catalog):
    c,ids,_,_=catalog
    album=create(c,media_ids=ids)
    cid=c.post('/api/album-collections',json={'name':'Cover test'}).json()['id']
    base=f'/api/album-collections/{cid}'
    c.post(base+'/albums',json={'album_ids':[album]})
    assert c.post(base+'/cover',json={'kind':'album','source_id':album,'media_id':ids[2]}).status_code==409
    r=c.post(base+'/cover',json={'kind':'album','source_id':album,'media_id':ids[0]})
    assert r.status_code==200,r.text
    framing={'media_id':ids[0],'mode':'fit','zoom':2,'x':.2,'y':-.3}
    assert c.patch(base+'/cover-framing',json=framing).status_code==200
    c.post(base+'/remove',json={'album_ids':[album]})
    c.delete(f'/api/albums/{album}')
    row=c.get(base).json()
    assert row['cover_id']==ids[0] and row['cover_framing']==framing
    assert c.get('/api/album-collections').json()['collections'][0]['cover_id']==ids[0]
    assert c.patch(base+'/cover-framing',json={**framing,'media_id':ids[2]}).status_code==409
    with database.connection() as db:
        db.execute('UPDATE import_items SET media_id=NULL WHERE media_id=%s',(ids[0],))
        db.execute('DELETE FROM media WHERE id=%s',(ids[0],))
    assert c.get(base).json()['cover_id'] is None


def test_adjust_automatic_collection_cover_pins_it(catalog):
    c,ids,_,_=catalog
    album=create(c,media_ids=ids)
    cid=c.post('/api/album-collections',json={'name':'Automatic'}).json()['id']
    base=f'/api/album-collections/{cid}'
    c.post(base+'/albums',json={'album_ids':[album]})
    assert c.patch(base+'/cover-framing',json={'media_id':ids[0]}).status_code==200
    c.post(base+'/remove',json={'album_ids':[album]})
    assert c.get(base).json()['cover_id']==ids[0]


def test_person_cover_is_pinned_and_validated(catalog,monkeypatch):
    from app.albums import collections
    c,ids,_,_=catalog
    pilot='a'*32
    async def read(*args):
        return pilot,{'people':[{'person_id':7,'name':'Person','cover_media_id':ids[0]}]}
    monkeypatch.setattr(collections,'people_read',read)
    cid=c.post('/api/album-collections',json={'name':'People cover'}).json()['id']
    base=f'/api/album-collections/{cid}'
    data={'kind':'person','source_id':7,'media_id':ids[0],'pilot':pilot}
    assert c.post(base+'/cover',json=data).status_code==409
    c.post(base+'/people',json={'pilot':pilot,'person_ids':[7]})
    assert c.post(base+'/cover',json={**data,'media_id':ids[2]}).status_code==409
    r=c.post(base+'/cover',json=data)
    assert r.status_code==200,r.text
    c.post(base+'/remove-people',json={'pilot':pilot,'person_ids':[7]})
    assert c.get(base).json()['cover_id']==ids[0]


def test_collection_lifecycle_preserves_albums_and_media(catalog):
    client, ids, _, _ = catalog
    a = create(client, name='A', media_ids=ids)
    b = create(client, name='B', media_ids=[ids[0]])
    response = client.post('/api/album-collections', json={'name':'Family','description':'Trips'})
    assert response.status_code == 200
    cid = response.json()['id']
    base = f'/api/album-collections/{cid}'
    assert client.get(base).json()['albums'] == []
    assert client.get('/api/album-collections').json()['collections'][0]['album_count'] == 0
    assert client.post(base+'/albums', json={'album_ids':[a,a,b]}).json() == {'added':2}
    assert client.post(base+'/albums', json={'album_ids':[a]}).json() == {'added':0}
    assert [row['id'] for row in client.get(base).json()['albums']] == [a,b]
    index = client.get('/api/album-collections').json()['collections'][0]
    assert index['album_count'] == 2 and index['cover_id'] == ids[0]
    assert client.patch(base, json={'name':'Renamed','description':'New'}).json()['name'] == 'Renamed'
    assert client.post(base+'/remove', json={'album_ids':[a]}).json() == {'removed':1}
    assert members(client,a) == ids
    second = client.post('/api/album-collections',json={'name':'Second'}).json()['id']
    assert client.post(f'/api/album-collections/{second}/albums',json={'album_ids':[b]}).json() == {'added':1}
    assert client.delete(base).status_code == 200
    assert members(client,b) == [ids[0]]
    assert len(client.get(f'/api/album-collections/{second}').json()['albums']) == 1
    assert client.delete(f'/api/albums/{b}').status_code == 200
    assert client.get(f'/api/album-collections/{second}').json()['albums'] == []
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n'] == len(ids)


def test_collection_validates_members_atomically_and_rejects_nesting(catalog):
    client, ids, _, _ = catalog
    a = create(client, media_ids=ids)
    cid = client.post('/api/album-collections',json={'name':'Empty'}).json()['id']
    base = f'/api/album-collections/{cid}'
    assert client.post(base+'/albums',json={'album_ids':[a,999999]}).status_code == 404
    assert client.get(base).json()['albums'] == []
    for body in ({'collection_ids':[cid]}, {'album_ids':[a],'collection_ids':[cid]}, {'album_ids':[True]}, {'album_ids':[-1]}):
        assert client.post(base+'/albums',json=body).status_code == 422
    assert client.post('/api/album-collections',json={'name':'  '}).status_code == 422
    assert client.post('/api/album-collections',json={'name':'Bad','children':[cid]}).status_code == 422
    assert client.post('/api/album-collections/999999/albums',json={'album_ids':[a]}).status_code == 404


def test_four_levels_moves_and_child_preservation(catalog):
    client, ids, _, _ = catalog
    def create_collection(name,parent=None):
        response=client.post('/api/album-collections',json={'name':name,'parent_id':parent})
        assert response.status_code==200,response.text
        return response.json()['id']
    one=create_collection('C1');two=create_collection('C2',one)
    three=create_collection('C3',two);four=create_collection('C4',three)
    assert client.post('/api/album-collections',json={'name':'C5','parent_id':four}).status_code==409
    aid=create(client,media_ids=ids)
    assert client.post(f'/api/album-collections/{four}/albums',json={'album_ids':[aid]}).status_code==200
    leaf=client.get(f'/api/album-collections/{four}').json()
    assert leaf['depth']==4 and [row['id'] for row in leaf['breadcrumbs']]==[one,two,three,four]
    assert leaf['collections']==[]
    assert [row['id'] for row in client.get('/api/album-collections').json()['collections']]==[one,two,three,four]
    for parent in [one,two,four]:
        assert client.post(f'/api/album-collections/{one}/parent',json={'parent_id':parent}).status_code==409
    other=create_collection('Other')
    # C1 itself would fit at level 2, but C4 would then be at level 5.
    assert client.post(f'/api/album-collections/{one}/parent',json={'parent_id':other}).status_code==409
    assert client.post(f'/api/album-collections/{two}/parent',json={'parent_id':other}).status_code==200
    assert client.get(f'/api/album-collections/{one}').json()['collections']==[]
    assert client.get(f'/api/album-collections/{four}').json()['depth']==4
    assert client.get(f'/api/album-collections/{four}/child-options').json()['collections']==[]
    assert client.delete(f'/api/album-collections/{two}').status_code==200
    assert client.get(f'/api/album-collections/{three}').json()['parent_id'] is None
    assert client.get(f'/api/album-collections/{four}').json()['depth']==2
    assert members(client,aid)==ids
    assert client.post(f'/api/album-collections/{four}/parent',json={'parent_id':None}).status_code==200
    assert client.get(f'/api/album-collections/{four}').json()['depth']==1


def test_concurrent_opposite_moves_cannot_create_cycle(catalog):
    from concurrent.futures import ThreadPoolExecutor
    client,*_=catalog
    a,b=[client.post('/api/album-collections',json={'name':name}).json()['id'] for name in ['A','B']]
    def move(pair):
        child,parent=pair
        return client.post(f'/api/album-collections/{child}/parent',json={'parent_id':parent}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(move,[(a,b),(b,a)]))==[200,409]


def test_album_visibility_and_last_membership(catalog):
    client, ids, _, _ = catalog
    aid = create(client, name='Visible', media_ids=ids)
    url = f'/api/albums/{aid}'
    def visibility():
        return client.get(url).json()['show_on_albums']
    assert visibility() is True
    payload = {'name':'Hidden','description':'','show_on_albums':False}
    assert client.patch(url,json=payload).status_code == 409
    assert client.get(url).json()['name'] == 'Visible'
    a,b = [client.post('/api/album-collections',json={'name':n}).json()['id'] for n in ['A','B']]
    for cid in [a,b]:
        assert client.post(f'/api/album-collections/{cid}/albums',json={'album_ids':[aid]}).status_code == 200
    assert client.patch(url,json=payload).status_code == 200
    assert visibility() is False
    assert any(row['id']==aid for row in client.get('/api/albums').json()['albums'])
    assert client.get(f'/api/album-collections/{a}').json()['albums'][0]['id'] == aid
    assert client.post(f'/api/album-collections/{a}/albums',json={'album_ids':[aid]}).json()['added'] == 0
    assert visibility() is False
    assert client.post(f'/api/album-collections/{a}/remove',json={'album_ids':[aid]}).status_code == 200
    assert visibility() is False
    assert client.delete(f'/api/album-collections/{b}').status_code == 200
    assert visibility() is True
    assert members(client,aid) == ids


def test_concurrent_final_removals_restore_visibility(catalog):
    from concurrent.futures import ThreadPoolExecutor
    client,ids,_,_=catalog
    aid=create(client,media_ids=ids)
    cids=[client.post('/api/album-collections',json={'name':n}).json()['id'] for n in ['A','B']]
    for cid in cids:
        client.post(f'/api/album-collections/{cid}/albums',json={'album_ids':[aid]})
    assert client.patch(f'/api/albums/{aid}',json={'name':'Hidden','show_on_albums':False}).status_code==200
    def remove(cid):
        return client.post(f'/api/album-collections/{cid}/remove',json={'album_ids':[aid]}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(remove,cids))==[200,200]
    assert client.get(f'/api/albums/{aid}').json()['show_on_albums'] is True


def test_deleting_parent_preserves_hidden_child_album(catalog):
    client,ids,_,_=catalog
    aid=create(client,media_ids=ids)
    parent=client.post('/api/album-collections',json={'name':'Parent'}).json()['id']
    child=client.post('/api/album-collections',json={'name':'Child','parent_id':parent}).json()['id']
    client.post(f'/api/album-collections/{child}/albums',json={'album_ids':[aid]})
    client.patch(f'/api/albums/{aid}',json={'name':'Hidden','show_on_albums':False})
    assert client.delete(f'/api/album-collections/{parent}').status_code==200
    assert client.get(f'/api/albums/{aid}').json()['show_on_albums'] is False
    assert client.post(f'/api/album-collections/{child}/remove',json={'album_ids':[aid]}).status_code==200
    assert client.get(f'/api/albums/{aid}').json()['show_on_albums'] is True


def test_live_people_links_are_namespaced_and_non_destructive(catalog,monkeypatch):
    from app.albums import collections
    from fastapi import HTTPException
    client,ids,_,_=catalog
    pilot='a'*32
    people=[{'person_id':7,'name':'A','photo_count':1,'cover_detection_id':3}]
    async def read(request,path,expected=None):
        if expected and expected!=pilot:raise HTTPException(409,'Catalog changed')
        if path=='named-people':return pilot,{'people':people}
        return pilot,{'name':people[0]['name'],'items':[{'id':ids[0]}]}
    monkeypatch.setattr(collections,'people_read',read)
    a,b=[client.post('/api/album-collections',json={'name':n}).json()['id'] for n in ['A','B']]
    base=f'/api/album-collections/{a}'
    payload={'pilot':pilot,'person_ids':[7,7]}
    assert client.get(base+'/person-options').json()['people']==people
    assert client.post(base+'/people',json=payload).json()=={'added':1}
    assert client.post(base+'/people',json=payload).json()=={'added':0}
    assert client.post(f'/api/album-collections/{b}/people',json=payload).json()=={'added':1}
    assert client.get(base+'/person-options').json()['included']==[7]
    assert client.get(base).json()['person_count']==1
    assert client.post(base+'/people',json={'pilot':pilot,'person_ids':[7,8]}).status_code==409
    assert client.post(base+'/people',json={'pilot':pilot,'person_ids':[True]}).status_code==422
    people[0].update(name='Renamed',photo_count=2)
    assert client.get(base+'/people').json()['people'][0]['name']=='Renamed'
    assert client.get(base+'/people/7/photos',params={'pilot':pilot}).json()['items']==[{'id':ids[0]}]
    assert client.get(base+'/people/7/photos',params={'pilot':'b'*32}).status_code==404
    client.post('/api/login',json={'username':'reader','password':'fixture'})
    assert client.get(base+'/people').status_code==403
    assert client.get(base+'/person-options').status_code==403
    assert client.get(base+'/people/7/photos',params={'pilot':pilot}).status_code==403
    assert client.post(base+'/people',json=payload).status_code==403
    assert client.post(base+'/remove-people',json=payload).status_code==403
    client.post('/api/login',json={'username':'admin','password':'fixture'})
    async def unavailable(*args,**kwargs):raise HTTPException(503,'People is temporarily unavailable')
    monkeypatch.setattr(collections,'people_read',unavailable)
    unavailable_result=client.get(base+'/people').json()
    assert unavailable_result['warning'] and unavailable_result['people'][0]['available'] is False
    monkeypatch.setattr(collections,'people_read',read)
    people.clear()
    assert client.get(base+'/people').json()['people'][0]['available'] is False
    assert client.post(base+'/remove-people',json=payload).json()=={'removed':1}
    assert client.get(base+'/people/7/photos',params={'pilot':pilot}).status_code==404
    assert client.delete(f'/api/album-collections/{b}').status_code==200
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM album_collection_people').fetchone()['n']==0
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n']==len(ids)


def test_card_order_is_scoped_persistent_and_validated(catalog):
    client,ids,_,_=catalog
    def collection(name,parent=None):
        return client.post('/api/album-collections',json={'name':name,'parent_id':parent}).json()['id']
    a,b,c=[collection(n) for n in ['A','B','C']]
    def move(parent,kind,source,target,**extra):
        return client.post('/api/album-collections/'+('order/root' if parent is None else f'{parent}/order'),json={'kind':kind,'id':source,'target_id':target,**extra})
    assert move(None,'collection',c,a).json()['order']==[{'id':c},{'id':a},{'id':b}]
    assert [r['id'] for r in client.get('/api/album-collections').json()['collections']]==[c,a,b]
    x,y=[collection(n,a) for n in ['X','Y']]
    assert move(a,'collection',y,x).status_code==200
    assert [r['id'] for r in client.get(f'/api/album-collections/{a}').json()['collections']]==[y,x]
    assert move(a,'collection',b,x).status_code==409
    assert move(None,'collection',x,b).status_code==200
    assert [r['id'] for r in client.get(f'/api/album-collections/{a}').json()['collections']]==[y,x]
    assert client.post(f'/api/album-collections/{y}/parent',json={'parent_id':b}).status_code==200
    assert move(a,'collection',y,x).status_code==409
    albums=[create(client,name=n,media_ids=ids) for n in ['One','Two','Three']]
    for parent in [a,b]:client.post(f'/api/album-collections/{parent}/albums',json={'album_ids':albums})
    original_root=[r['id'] for r in client.get('/api/albums').json()['albums']]
    assert move(a,'album',albums[0],albums[2],after=True).status_code==200
    assert [r['id'] for r in client.get(f'/api/album-collections/{a}').json()['albums']]==albums[1:]+albums[:1]
    assert [r['id'] for r in client.get(f'/api/album-collections/{b}').json()['albums']]==albums
    assert [r['id'] for r in client.get('/api/albums').json()['albums']]==original_root
    assert move(None,'album',albums[0],albums[1]).status_code==422
    pilot='a'*32;other='b'*32
    with database.connection() as db:
        for parent in [a,b]:
            db.execute('INSERT INTO album_collection_people VALUES(%s,%s,7,1),(%s,%s,7,2)',(parent,pilot,parent,other))
    result=move(a,'person',7,7,pilot=other,target_pilot=pilot)
    assert result.json()['order']==[{'id':7,'pilot':other},{'id':7,'pilot':pilot}]
    with database.connection() as db:
        assert [r['pilot'] for r in db.execute('SELECT pilot FROM album_collection_people WHERE collection_id=%s ORDER BY position',(b,)).fetchall()]==[pilot,other]
    assert move(a,'person',7,7).status_code==422
    client.post('/api/login',json={'username':'reader','password':'fixture'})
    assert move(a,'person',7,7,pilot=other,target_pilot=pilot).status_code==403


def test_collection_visibility_independent_of_parent_and_restored(catalog):
    client,*_=catalog
    def create_collection(name,parent=None):
        return client.post('/api/album-collections',json={'name':name,'parent_id':parent}).json()['id']
    a,b,c=[create_collection(n) for n in ['A','B','C']]
    def visible():return [r['id'] for r in client.get('/api/album-collections').json()['collections']]
    url=f'/api/album-collections/{c}'
    assert client.patch(url,json={'name':'Changed','show_on_albums':False}).status_code==409
    assert client.get(url).json()['name']=='C'
    assert client.post(url+'/parent',json={'parent_id':a}).status_code==200
    assert c in visible()
    assert client.patch(url,json={'name':'C','show_on_albums':False}).status_code==200
    assert c not in visible()
    assert client.get(f'/api/album-collections/{a}').json()['collections'][0]['id']==c
    assert client.post(url+'/parent',json={'parent_id':b}).status_code==200
    assert c not in visible()
    assert client.post(url+'/parent',json={'parent_id':None}).status_code==200
    assert c in visible()
    client.post(url+'/parent',json={'parent_id':a})
    client.patch(url,json={'name':'C','show_on_albums':False})
    assert client.delete(f'/api/album-collections/{a}').status_code==200
    assert c in visible() and client.get(url).json()['parent_id'] is None
    assert client.post('/api/album-collections',json={'name':'Lost','show_on_albums':False}).status_code==409


def test_hide_collection_racing_detach_cannot_make_it_unreachable(catalog):
    from concurrent.futures import ThreadPoolExecutor
    client,*_=catalog
    a=client.post('/api/album-collections',json={'name':'Parent'}).json()['id']
    b=client.post('/api/album-collections',json={'name':'Child','parent_id':a}).json()['id']
    base=f'/api/album-collections/{b}'
    with ThreadPoolExecutor(max_workers=2) as pool:
        hide=pool.submit(client.patch,base,json={'name':'Child','show_on_albums':False})
        detach=pool.submit(client.post,base+'/parent',json={'parent_id':None})
        assert hide.result().status_code in (200,409)
        assert detach.result().status_code==200
    state=client.get(base).json()
    assert state['parent_id'] is None and state['show_on_albums'] is True
