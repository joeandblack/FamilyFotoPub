"""On-demand desktop server, with a cooperative local stop signal."""
import asyncio
from ..config import DATA, load_config
from .server import server_options
from .stop_watch import watch_stop
import uvicorn

STOP = DATA / 'DESKTOP-SERVER-STOP'


def serve_restored_people(config):
    """A library moved here from another computer (app.migration.move_in) keeps its People as the
    recovery launcher serves them: viewable at once, editable once People is set up here. The Mac
    service does the same (app.service.macos)."""
    if config.get('recovery', {}).get('state') != 'activated':
        return
    from .. import api
    from ..config import ROOT
    from ..migration.launch import restored_people
    restored_people(api.app, config, DATA, ROOT)


async def serve():
    config = load_config()
    serve_restored_people(config)
    server = uvicorn.Server(uvicorn.Config('app.api:app', timeout_graceful_shutdown=30,
                                         **server_options(config)))
    watcher = asyncio.create_task(watch_stop(server, STOP))
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        from face_detect.api.service import shutdown
        await asyncio.to_thread(shutdown)


def main():
    from ..backup.admission import hold_process_lease
    hold_process_lease(DATA)
    DATA.mkdir(exist_ok=True)
    STOP.unlink(missing_ok=True)
    asyncio.run(serve())


if __name__ == '__main__':
    main()
