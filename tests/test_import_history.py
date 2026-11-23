from unittest.mock import Mock
from app.imports.history import history
from app.imports.ignore import IGNORE_SQL_PATTERN


def test_history_includes_empty_and_cancelled_jobs_and_excludes_ignored(monkeypatch):
    monkeypatch.setattr('app.photos.duplicates.all_counts',lambda db:{})
    db=Mock()
    db.execute.return_value.fetchall.side_effect=[
        [{'id':3,'state':'running'},{'id':2,'state':'cancelled'},{'id':1,'state':'completed-review-required'}],
        [{'job_id':3,'state':'pending','count':2,'bytes':300},
         {'job_id':1,'state':'imported','media_kind':'photo','count':3,'bytes':700},
         {'job_id':1,'state':'imported','media_kind':'video','count':1,'bytes':300},
         {'job_id':1,'state':'duplicate','count':1,'bytes':200},
         {'job_id':1,'state':'unsupported','count':1,'bytes':50}],
        [{'job_id':1,'metadata_state':'done','conversion_state':'pending','count':2,'conversion_format':'.mts','conversion_pending':1},
         {'job_id':3,'metadata_state':'pending','conversion_state':None,'count':1}]]
    result=history(db)
    assert [row['id'] for row in result]==[3,2,1]
    assert result[1]['total']==0 and result[1]['counts']=={}
    assert result[1]['mts_pending']==0 and result[2]['mts_pending']==1
    assert result[2]['conversion_pending']==1
    assert result[2]['conversion_pending_formats']=={'AVI':0,'FLV':0,'MTS':1}
    assert result[2]['total']==6 and result[2]['total_bytes']==1250
    assert result[2]['bytes_by_state']['imported']==1000
    assert db.execute.call_args_list[1].args[1][-1]==IGNORE_SQL_PATTERN
    assert result[2]['stages']=={'metadata':{'done':2},'conversion':{'pending':2},'duplicates':{}}
    assert result[2]['media_totals']['photo']=={'files':3,'bytes':700}
    assert result[2]['media_totals']['video']=={'files':1,'bytes':300}
    assert result[2]['media_totals']['unclassified_duplicates']=={'files':0,'bytes':0}
    assert 'm.first_import_job_id job_id' in db.execute.call_args.args[0]
