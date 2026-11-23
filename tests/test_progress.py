import json
from datetime import datetime, timezone
from app.imports import importer


def test_byte_progress_distinguishes_skips_and_failures(tmp_path, monkeypatch):
    class Result:
        def __init__(self, value): self.value = value
        def fetchone(self): return self.value
        def fetchall(self): return self.value
    class FakeDB:
        def execute(self, query, values):
            if 'SELECT * FROM jobs' in query:
                return Result({'started_at':datetime.now(timezone.utc), 'updated_at':datetime.now(timezone.utc), 'total_bytes':1000})
            return Result([{'state':state,'count':1,'bytes':size} for state,size in [('imported',300),('duplicate',200),('unsupported',100),('pending',150),('error',250),('ignored',50)]])
        def commit(self): pass
    monkeypatch.setattr(importer,'DATA',tmp_path)
    importer.publish(1,db=FakeDB())
    status=json.loads((tmp_path/'import-status.json').read_text())
    assert status['processed_bytes']==600
    assert status['total_bytes']==950 and status['total']==5
    assert 'ignored' not in status['counts']
    assert status['bytes_by_state']['imported']==300
    assert status['bytes_by_state']['duplicate']==200
    assert status['bytes_by_state']['unsupported']==100
    assert status['bytes_by_state']['pending']+status['bytes_by_state']['error']==400
