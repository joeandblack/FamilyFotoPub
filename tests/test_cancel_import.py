from unittest.mock import Mock
import pytest
from app.imports.cancel import cancel_scan


def test_only_unprocessed_queue_is_removed():
    db=Mock()
    db.execute.return_value.fetchone.side_effect=[{'available':True},{'source_root':'root','state':'paused-error','error':'scan error'},None]
    db.execute.return_value.fetchall.return_value=[{'state':'pending','count':10}]
    result=cancel_scan(db,7,'root')
    assert result['cleared_counts'][0]['count']==10
    queries=[call.args[0] for call in db.execute.call_args_list]
    assert any('DELETE FROM import_items' in q for q in queries)
    assert not any('DELETE FROM media' in q for q in queries)
    assert any("state='cancelled',error=NULL" in q for q in queries)


@pytest.mark.parametrize('answers',[
    [{'available':False}],
    [{'available':True},{'source_root':'other','state':'paused-error'}],
    [{'available':True},{'source_root':'root','state':'paused-error','error':'x'},{'exists':1}],
])
def test_active_or_wrong_or_processed_jobs_are_untouched(answers):
    db=Mock();db.execute.return_value.fetchone.side_effect=answers
    with pytest.raises(RuntimeError):cancel_scan(db,7,'root')
    assert not any(call.args[0].startswith(('DELETE','UPDATE')) for call in db.execute.call_args_list)
