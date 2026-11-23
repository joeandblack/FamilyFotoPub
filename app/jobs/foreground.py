"""Cooperative foreground activity signal for separate background processes.

This is admission guidance, not preemption. A marker contains no request data.
Each serving process pulses the same marker during requests, including streaming.
"""
import asyncio
import os
import time
from pathlib import Path

from ..config import DATA

MARKER = DATA / 'foreground-activity'
PULSE_SECONDS = 0.5
QUIET_SECONDS = 1.0


def should_yield(path=None, *, now=None, quiet_seconds=QUIET_SECONDS):
    """Missing/unreadable signals fail closed until a web process creates one."""
    try:
        age = (time.time() if now is None else now) - Path(path or MARKER).stat().st_mtime
        return age < quiet_seconds  # Future timestamps also defer admission.
    except OSError:
        return True


class ForegroundActivityMiddleware:
    def __init__(self, app, marker=None, interval=PULSE_SECONDS):
        self.app = app
        self.marker = Path(marker or MARKER)
        self.interval = interval
        self.last_pulse = 0.0

    def pulse(self):
        now = time.monotonic()
        if now - self.last_pulse < self.interval:
            return
        try:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.touch(exist_ok=True)
            os.utime(self.marker, None)
            self.last_pulse = now
        except OSError:
            # Foreground requests must not fail because background scheduling
            # cannot write its marker. The worker also checks storage health.
            pass

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path','').startswith('/api/people-preview/jobs') or (
            scope.get('method') == 'GET' and scope.get('path') in ('/api/status', '/api/faces/status', '/api/people/status', '/api/people-preview/import-status')
        ):
            return await self.app(scope, receive, send)
        self.pulse()

        async def heartbeat():
            while True:
                await asyncio.sleep(self.interval)
                self.pulse()

        task = asyncio.create_task(heartbeat())
        try:
            return await self.app(scope, receive, send)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.pulse()
