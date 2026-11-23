import asyncio
import os
import time

from app.jobs.foreground import ForegroundActivityMiddleware, should_yield


def test_missing_recent_old_and_future_signal(tmp_path):
    path = tmp_path / 'signal'
    assert should_yield(path)
    path.touch()
    assert should_yield(path)
    os.utime(path, (100, 100))
    assert not should_yield(path, now=103)
    assert should_yield(path, now=99)


def test_activity_continues_until_stream_finishes(tmp_path):
    async def run():
        path = tmp_path / 'signal'
        samples = []

        async def app(scope, receive, send):
            samples.append(path.stat().st_mtime)
            await asyncio.sleep(.06)
            samples.append(path.stat().st_mtime)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await asyncio.sleep(.06)
            samples.append(path.stat().st_mtime)
            await send({'type': 'http.response.body', 'body': b'done'})

        async def send(message):
            pass

        middleware = ForegroundActivityMiddleware(app, path, interval=.01)
        await middleware({'type': 'http', 'method': 'GET', 'path': '/api/media/1/file'}, None, send)
        assert samples[0] < samples[1] < samples[2]
        ended = path.stat().st_mtime
        await asyncio.sleep(.04)
        assert path.stat().st_mtime == ended
    asyncio.run(run())


def test_status_poll_does_not_starve_worker(tmp_path):
    async def run():
        path = tmp_path / 'signal'
        called = []
        async def app(scope, receive, send):
            called.append(True)
        for endpoint in ('/api/status', '/api/faces/status', '/api/people/status', '/api/people-preview/import-status', '/api/people-preview/jobs/scan-one'):
            await ForegroundActivityMiddleware(app, path)(
                {'type': 'http', 'method': 'GET', 'path': endpoint}, None, None)
        assert called and not path.exists()
    asyncio.run(run())


def test_exception_cancels_heartbeat(tmp_path):
    async def run():
        path = tmp_path / 'signal'
        async def app(scope, receive, send):
            raise RuntimeError('fixture')
        middleware = ForegroundActivityMiddleware(app, path, interval=.01)
        try:
            await middleware({'type': 'http', 'method': 'GET', 'path': '/'}, None, None)
        except RuntimeError:
            pass
        stamp = path.stat().st_mtime
        await asyncio.sleep(.04)
        assert path.stat().st_mtime == stamp
    asyncio.run(run())
