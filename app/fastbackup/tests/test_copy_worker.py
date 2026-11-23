import multiprocessing as mp
from multiprocessing.util import Finalize
import os
import time

import pytest

from app.fastbackup.copy_worker import copy_in_worker
from app.fastbackup import copy_worker
from app.backup.diagnostics import ExplainedFailure
from app.fastbackup.tests.test_full_copy import fixture, Guard


class SlowGuard(Guard):
    def check(self, **kwargs):
        time.sleep(30)


class SlowExitGuard(Guard):
    def __init__(self, requirements):
        super().__init__(requirements)
        # Real child teardown after the completion message, not slow media copying.
        if not getattr(type(self), 'registered', False):
            type(self).registered = True
            Finalize(None, time.sleep, args=(6,), exitpriority=0)


class FailedExitGuard(Guard):
    def __init__(self, requirements):
        super().__init__(requirements)
        if not getattr(type(self), 'registered', False):
            type(self).registered = True
            Finalize(None, os._exit, args=(23,), exitpriority=0)


class AbruptGuard(Guard):
    def check(self, **kwargs):
        os._exit(24)


def test_slow_clean_exit_after_completion_is_accepted(tmp_path):
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    events = []
    result = copy_in_worker(*paths, offline_confirmed=True, progress=events.append,
                            guard_factory=SlowExitGuard, timeout=20)
    assert result['files'] == 3 and result['content_verified']
    assert (paths[2] / 'TREE-COMPLETE.json').exists()
    waiting = [event for event in events if event['phase'] == 'worker-exit']
    assert waiting and all(event['elapsed_seconds'] >= 5 for event in waiting)
    assert events[-1]['phase'] == 'worker-exited' and events[-1]['exit_code'] == 0
    assert events[-1]['elapsed_seconds'] >= 5
    assert {p.pid for p in mp.active_children()} == before


def test_abrupt_exit_without_completion_reports_code(tmp_path):
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    with pytest.raises(ExplainedFailure, match='without completion .*exit code 24'):
        copy_in_worker(*paths, offline_confirmed=True, guard_factory=AbruptGuard, timeout=20)
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()
    assert {p.pid for p in mp.active_children()} == before


def test_nonzero_exit_after_completion_is_never_success(tmp_path):
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    with pytest.raises(ExplainedFailure, match='exit code 23'):
        copy_in_worker(*paths, offline_confirmed=True,
                       guard_factory=FailedExitGuard, timeout=20)
    assert (paths[2] / 'TREE-COMPLETE.json').exists()
    assert {p.pid for p in mp.active_children()} == before


def test_stalled_exit_is_bounded_and_reaped(tmp_path, monkeypatch):
    monkeypatch.setattr(copy_worker, 'WORKER_EXIT_SECONDS', 0.3)
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    events = []
    with pytest.raises(ExplainedFailure, match='did not exit within 0.3 seconds'):
        copy_in_worker(*paths, offline_confirmed=True, guard_factory=SlowExitGuard,
                       progress=events.append, timeout=20)
    assert not any(event['phase'] in {'worker-exit', 'worker-exited'} for event in events)
    assert (paths[2] / 'TREE-COMPLETE.json').exists()
    assert {p.pid for p in mp.active_children()} == before


def test_cancellation_during_exit_never_accepts_completion(tmp_path):
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    events = []
    with pytest.raises(InterruptedError, match='cancelled while waiting for worker exit'):
        copy_in_worker(*paths, offline_confirmed=True, guard_factory=SlowExitGuard,
                       progress=events.append, timeout=20,
                       cancelled=lambda: any(event['phase'] == 'worker-exit' for event in events))
    assert (paths[2] / 'TREE-COMPLETE.json').exists()
    assert {p.pid for p in mp.active_children()} == before


def test_overall_deadline_still_applies_during_exit(tmp_path, monkeypatch):
    real = copy_worker._confirm_exit
    def short_remaining_budget(process, deadline, cancelled, progress):
        return real(process, time.monotonic() + 0.3, cancelled, progress)
    monkeypatch.setattr(copy_worker, '_confirm_exit', short_remaining_budget)
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    with pytest.raises(TimeoutError, match='deadline exceeded while waiting for worker exit'):
        copy_in_worker(*paths, offline_confirmed=True, guard_factory=SlowExitGuard, timeout=20)
    assert {p.pid for p in mp.active_children()} == before


def test_exit_wait_is_logged_and_failures_keep_safe_diagnostics():
    from app.backup.admin_runner import progress_note
    from app.fastbackup.diagnostics import describe
    assert 'shutting down' in progress_note({'phase': 'worker-exit', 'state': 'waiting'})
    note = progress_note({'phase': 'worker-exited', 'elapsed_seconds': 6.125, 'exit_code': 0})
    assert '6.125 seconds' in note and 'exit code 0' in note and 'finalization remains' in note
    error = ExplainedFailure('Media copy reported completion, but the worker exited unsuccessfully (exit code 23)')
    assert describe(error)['message'] == str(error)


def test_worker_completion_and_no_children(tmp_path):
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    events = []
    result = copy_in_worker(*paths, offline_confirmed=True, guard_factory=Guard, timeout=15,
                            progress=events.append)
    assert result['files'] == 3 and result['content_verified']
    assert events[-1]['phase'] == 'worker-exited' and events[-1]['exit_code'] == 0
    assert events[-1]['elapsed_seconds'] >= 0
    assert {p.pid for p in mp.active_children()} == before


def test_quick_exit_has_no_waiting_heartbeat(monkeypatch):
    class Process:
        exitcode = 0
        alive = True
        def is_alive(self):
            return self.alive
        def join(self, timeout):
            self.alive = False
    ticks = iter([0, 0.1, 0.1, 0.2])
    monkeypatch.setattr(copy_worker.time, 'monotonic', lambda: next(ticks))
    events = []
    assert copy_worker._confirm_exit(Process(), 30, lambda: False, events.append) == 0.2
    assert events == []


def test_blocked_worker_deadline_reaps_child(tmp_path):
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        copy_in_worker(*paths, offline_confirmed=True, guard_factory=SlowGuard, timeout=1)
    assert time.monotonic() - started < 8
    assert {p.pid for p in mp.active_children()} == before
    assert not (paths[2] / 'TREE-COMPLETE.json').exists()


def test_worker_cancellation_reaps_child(tmp_path):
    paths = fixture(tmp_path)
    before = {p.pid for p in mp.active_children()}
    with pytest.raises(InterruptedError):
        copy_in_worker(*paths, offline_confirmed=True, guard_factory=SlowGuard,
                       cancelled=lambda: True)
    assert {p.pid for p in mp.active_children()} == before
