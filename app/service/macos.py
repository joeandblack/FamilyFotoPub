"""Native Mac HTTP entry point with cooperative media-worker shutdown."""
import asyncio
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse
import uvicorn
from ..api import app
from ..auth import accounts
from ..config import load_config
from ..database import migrate, close_pools
from .server import server_options
from .macos_lifecycle import stopping, request_pause, drain_workers, PAUSE_WEB
from installer.macos.manage import LABEL, settings


def allowed(method, path):
    return True


@asynccontextmanager
async def lifespan(app):
    settings()
    accounts(load_config())
    migrate()
    from ..uploads import start_dispatcher
    stop = start_dispatcher()
    from face_detect.api.service import start_with_app,shutdown
    start_with_app()
    try:
        yield
    finally:
        # A backup stops only this server. Touching the stop marker would take the database down too.
        request_pause(stop=not PAUSE_WEB.exists())
        await asyncio.to_thread(shutdown)
        stop.set()
        thread = getattr(stop, 'worker_thread', None)
        if thread:
            await asyncio.to_thread(thread.join)
        await drain_workers()
        close_pools()


app.router.lifespan_context = lifespan


@app.middleware('http')
async def mac_guard(request, call_next):
    path = request.url.path
    if path == '/api/macos-health' and request.method == 'GET':
        return JSONResponse({'instance':LABEL, 'mode':'macos-media'})
    if not allowed(request.method, path):
        return JSONResponse({'detail':'Face recognition is not installed on this Mac.'}, status_code=503)
    if stopping() and request.method not in ('GET', 'HEAD', 'OPTIONS') and not path.endswith('/pause') and path not in ('/api/auth/logout','/api/auth/activity'):
        return JSONResponse({'detail':'FamilyFoto is stopping. Media work is pausing safely; wait before restarting.'}, status_code=503)
    return await call_next(request)


def restored_library(config):
    """A library moved here from another computer (app.migration.move_in) keeps its People as
    the recovery launcher serves them, rather than through a fresh install's People service."""
    return config.get('recovery', {}).get('state') == 'activated'


if __name__ == '__main__':
    config = load_config()
    if restored_library(config):
        from ..config import DATA, ROOT
        from ..migration.launch import restored_people
        restored_people(app, config, DATA, ROOT)
    uvicorn.run(app, **server_options(config))
