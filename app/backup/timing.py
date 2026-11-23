"""Where a capture's time goes: wall seconds per phase, and per kind of work inside the copy.

Numbers only, never a file name, so the record can sit unencrypted in the backup folder.
The copy's own figures overlap nothing: reading the source, hashing, writing, flushing to
disk, checkpoints, reading back and publishing are timed apart, and what is left of the
copy phase is per-file bookkeeping.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import time


FORMAT = 'familyfoto-capture-timing-v1'
RECORD = 'capture-timing.json'


class Timing:
    def __init__(self, path=None, **facts):
        self.path = path
        self.facts = facts
        self.phases = {}
        self.work = {}
        self.started = datetime.now(timezone.utc).isoformat()
        self.unsaved = False

    def add(self, name, seconds):
        self.work[name] = self.work.get(name, 0.0) + seconds

    @contextmanager
    def measure(self, name):
        begun = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - begun)

    @contextmanager
    def phase(self, name):
        """Time one phase, and save the record when it ends, however it ends."""
        begun = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0.0) + time.perf_counter() - begun
            self.save()

    def report(self, **more):
        return {'format': FORMAT, 'started_at': self.started, **self.facts, **more,
                'phase_seconds': {name: round(value, 1) for name, value in self.phases.items()},
                'copy_seconds': {name: round(value, 1) for name, value in self.work.items()}}

    def save(self, **more):
        """Write the record. A failure here is swallowed: the record is a convenience, so it
        must never fail a good capture, nor replace the error that explains a failed one."""
        self.facts.update(more)
        if self.path is not None:
            from .recovery import save_private
            try:
                save_private(self.path, self.report())
            except OSError:
                self.unsaved = True


@contextmanager
def measured(timer, name):
    """`timer.measure(name)`, or nothing when there is no timer."""
    if timer is None:
        yield
    else:
        with timer.measure(name):
            yield
