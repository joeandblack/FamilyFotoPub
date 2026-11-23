"""Server-side client for the isolated People service, used by other routers."""
import httpx
from fastapi import HTTPException

from face_detect.api import people_preview_gateway as gateway


async def read(request, path, pilot=None):
    # Looked up on the module at call time so overrides of gateway.settings
    # (read-only previews, tests) take effect here too.
    cfg = gateway.settings()
    if not cfg:
        raise HTTPException(503, 'People is currently unavailable. Your collection links are unchanged.')
    if pilot is not None and pilot != cfg['pilot']:
        raise HTTPException(409, 'This link belongs to a different People catalog. Remove it and add the person again.')
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
            result = await client.get(f"http://127.0.0.1:{cfg['port']}/api/automatic-people/{path}",
                                      headers={'cookie': request.headers.get('cookie', '')})
    except httpx.HTTPError:
        raise HTTPException(503, 'People is taking longer to respond. Try again shortly; your links are unchanged.') from None
    if result.headers.get('x-familyfoto-pilot') != cfg['pilot']:
        raise HTTPException(503, 'People service is restarting. Try again shortly.')
    if result.status_code == 404:
        raise HTTPException(404, 'This person is no longer available')
    if not result.is_success:
        raise HTTPException(503, 'People is currently unavailable. Try again shortly.')
    return cfg['pilot'], result.json()
