from datetime import datetime, timedelta, timezone
from app.elapsed import active_seconds

START=datetime(2026,1,1,tzinfo=timezone.utc)
def event(seconds,state):
    return {'created_at':START+timedelta(seconds=seconds),'event':state}

def test_multiple_pauses_and_resumes():
    events=[event(0,'running'),event(60,'paused'),event(360,'running'),event(420,'paused'),event(1020,'running')]
    assert active_seconds(START,events,START+timedelta(seconds=1080))==180

def test_paused_and_finished_do_not_keep_counting():
    for state in ('paused','paused-error','completed-review-required'):
        assert active_seconds(START,[event(60,state)],START+timedelta(days=1))==60

def test_repeated_running_events_do_not_reset_or_double_count():
    assert active_seconds(START,[event(10,'running'),event(20,'running')],START+timedelta(seconds=30))==30

def test_scan_time_and_empty_event_history():
    assert active_seconds(START,[event(10,'running')],START+timedelta(seconds=30))==30
    assert active_seconds(START,[],START+timedelta(seconds=30))==30
