"""The restore's progress as lines a setup window can read: `@@ ` and one event as JSON.

Each platform's restore.py hands this to `Move` with `--progress-lines`, so the Windows and Mac
setup windows read the same thing (installer/windows/README.md, "The setup window"):

    @@ {"stage": "restored", "state": "running"}
    @@ {"phase": "copy", "files": 4210, "total_files": 12686, "bytes": 2990000000, "total_bytes": 8855629175}

A stage's start and end are always printed. Counts are printed at most once per `interval`
seconds, since a copy reports every file. Every other line a restore prints is plain text for
the window's details. Folder names come through in any language: the JSON keeps them as
written, so the child's output must be UTF-8 (PYTHONIOENCODING=utf-8).
"""
import json
import sys
import time

PREFIX = '@@ '


class ProgressLines:
    def __init__(self, stream=None, interval=1.0, clock=time.monotonic):
        self.stream, self.interval, self.clock = stream, interval, clock
        self.shown = None

    def __call__(self, event):
        if not isinstance(event, dict):
            return
        if 'stage' not in event:
            moment = self.clock()
            if self.shown is not None and moment - self.shown < self.interval:
                return
            self.shown = moment
        stream = self.stream or sys.stdout
        stream.write(PREFIX + json.dumps(event, ensure_ascii=False, separators=(', ', ': '), default=str) + '\n')
        stream.flush()


def parse_line(line):
    """The event on a progress line, or None for a line of plain text."""
    if not line.startswith(PREFIX):
        return None
    try:
        event = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    return event if isinstance(event, dict) else None
