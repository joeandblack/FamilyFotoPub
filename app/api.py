"""Application assembly: builds the FastAPI app from its routers and middleware."""
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .config import ROOT, load_config
from .database import migrate, close_pools
from .auth import accounts
from .release import display_version


@asynccontextmanager
async def lifespan(app):
    accounts(load_config())
    migrate()
    from .uploads import start_dispatcher
    stop=start_dispatcher()
    from face_detect.api.service import start_with_app
    start_with_app()
    try:yield
    finally:
        stop.set()
        from face_detect.api.service import shutdown
        await asyncio.to_thread(shutdown)
        close_pools()


async def validation_failed(request, error):
    """FastAPI's own 422 answer repeats the submitted input, which can be a password (sign-in, or
    the administrator password that protects a backup). Say where and what, never the value."""
    from fastapi.responses import JSONResponse
    details = [{name: item[name] for name in ('type', 'loc', 'msg') if name in item} for item in error.errors()]
    return JSONResponse({'detail': details}, status_code=422)


def create_app():
    """Registration order is significant: it decides which route matches first."""
    from .photos.duplicates_api import router as photo_duplicate_router
    from .jobs.foreground import ForegroundActivityMiddleware
    from .uploads.api import router as upload_router, settings_router as upload_settings_router
    from .albums import router as album_router
    from .albums.collections import router as album_collection_router
    from .library.downloads import router as download_router
    from face_detect.api.people_preview_gateway import router as people_preview_router
    from face_detect.api.service import router as face_service_router
    from .auth.api import protect, router as auth_router
    from .system_api import router as system_router
    from .library.api import router as media_router
    from .imports.api import router as imports_router
    from .backup.admin_api import router as backup_router

    app=FastAPI(title='FamilyFoto',version=display_version(),lifespan=lifespan)
    from fastapi.exceptions import RequestValidationError
    app.add_exception_handler(RequestValidationError, validation_failed)
    app.include_router(photo_duplicate_router)
    app.add_middleware(ForegroundActivityMiddleware)
    app.include_router(upload_router)
    app.include_router(upload_settings_router)
    app.include_router(album_router)
    app.include_router(album_collection_router)
    app.include_router(download_router)
    app.include_router(people_preview_router)
    app.include_router(face_service_router)
    app.middleware('http')(protect)
    for router in (auth_router,system_router,media_router,imports_router,backup_router):
        app.include_router(router)
    app.mount('/web',StaticFiles(directory=ROOT/'web'),name='web')
    app.mount('/face_detect/web',StaticFiles(directory=ROOT/'face_detect/web'),name='face-assets')
    return app


app=create_app()
