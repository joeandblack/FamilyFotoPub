import asyncio
from types import SimpleNamespace
from app.service.stop_watch import watch_stop


def test_local_stop_requests_graceful_exit(tmp_path):
    marker = tmp_path / 'stop'
    marker.touch()
    server = SimpleNamespace(should_exit=False)
    asyncio.run(watch_stop(server, marker))
    assert server.should_exit
    assert marker.exists()


def test_already_exiting_does_not_require_marker(tmp_path):
    server = SimpleNamespace(should_exit=True)
    asyncio.run(watch_stop(server, tmp_path / 'absent'))
    assert server.should_exit
