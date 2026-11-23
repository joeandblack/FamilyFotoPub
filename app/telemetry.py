from datetime import datetime, timezone
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
from .config import LOGS

class StageTimings:
    """Aggregate wall times without writing a log entry for every stage."""
    def __init__(self):
        self.started=time.monotonic()
        self.seconds={}
        self.calls={}

    @contextmanager
    def measure(self,name):
        start=time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name]=self.seconds.get(name,0)+time.perf_counter()-start
            self.calls[name]=self.calls.get(name,0)+1

    def snapshot(self):
        return {'updated':utcnow(),'elapsed_seconds':time.monotonic()-self.started,
                'seconds':dict(self.seconds),'calls':dict(self.calls)}

def logger(name):
    LOGS.mkdir(exist_ok=True)
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler = RotatingFileHandler(LOGS / f'{name}.log', maxBytes=8*1024*1024, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        log.addHandler(handler)
    return log

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def write_json(path: Path, value):
    from .backup.admission import guard_application_path
    guard_application_path(path)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.05)
