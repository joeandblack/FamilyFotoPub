from contextlib import contextmanager
from app.library import api


def test_large_gallery_page_and_last_page_clamp(monkeypatch):
    class DB:
        def execute(self,query,values=None):
            self.query=query
            self.values=values
            if 'LIMIT' in query:
                self.limit,self.offset=values[-2:]
            return self
        def fetchone(self):
            return {'total':51446,'bytes':1,'videos':0}
        def fetchall(self):
            if 'FROM jobs' in self.query: return []
            if 'capture_year' in self.query: return []
            return [{'id':i} for i in range(self.offset,min(self.offset+self.limit,51446))]
    db=DB()
    @contextmanager
    def connection(): yield db
    monkeypatch.setattr(api,'connection',connection)
    first=api.library_data()
    assert first['page_size']==1000 and first['pages']==52
    assert len(first['items'])==1000 and db.offset==0
    last=api.library_data(page=99999)
    assert last['page']==52 and len(last['items'])==446 and db.offset==51000
    assert api.library_data(page=-1)['page']==1
