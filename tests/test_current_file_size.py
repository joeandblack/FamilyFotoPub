import json
from contextlib import contextmanager
from types import SimpleNamespace
from app import system_api as api


def test_status_uses_inventory_size_without_source_access(tmp_path,monkeypatch):
    job={'id':5,'started_at':'2026-10-11T00:00:00+00:00','current':'F:/photos/clip.mov'}
    (tmp_path/'import-status.json').write_text(json.dumps(job))
    monkeypatch.setattr(api,'DATA',tmp_path)
    monkeypatch.setattr(api,'load_config',lambda:{'source':{},'destination':{},'io_mibps':30})
    calls=[]
    class DB:
        def execute(self,query,args):
            calls.append((query,args))
            return SimpleNamespace(fetchall=lambda:[],fetchone=lambda:{'size':4131247869})
    @contextmanager
    def connection():yield DB()
    monkeypatch.setattr(api,'connection',connection)
    assert api.status()['job']['current_size']==4131247869
    assert calls[-1][1]==(5,'F:/photos/clip.mov')
    job['current']=None
    (tmp_path/'import-status.json').write_text(json.dumps(job))
    assert api.status()['job']['current_size'] is None
