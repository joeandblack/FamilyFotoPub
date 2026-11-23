"""Same-origin authenticated bridge to an explicitly enabled isolated pilot."""
import json
import re
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from app.config import DATA
from app.config import ROOT
from app.auth.deps import administrator, reader

router = APIRouter()
SETTINGS = DATA / 'people-preview.json'


def settings():
    if not SETTINGS.exists():
        return None
    value = json.loads(SETTINGS.read_text())
    if value.get('enabled') is not True or value.get('backend')!='native':
        return None
    token, port = value.get('pilot'), value.get('port')
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('Invalid People pilot')
    if type(port) is not int or not 49152 <= port <= 65535:
        raise ValueError('Invalid People preview port')
    return {'pilot': token, 'port': port,
            'root': DATA / 'face_detect/profiles' / token}


@router.get('/people-preview')
def page(user=Depends(reader)):
    if not settings():
        raise HTTPException(404, 'People preview is not enabled')
    html = (ROOT/'face_detect/web/automatic-people-preview.html').read_text(encoding='utf-8')
    if user['role'] != 'admin':
        html += '<script type="module" src="/web/read-only-preview.mjs"></script>'
    return HTMLResponse(html)


@router.api_route('/api/people-preview', methods=['GET', 'POST'])
@router.api_route('/api/people-preview/{path:path}', methods=['GET', 'POST'])
async def bridge(request: Request, path: str = '', user=Depends(reader)):
    if request.method != 'GET':
        administrator(user)
    cfg = settings()
    if not cfg:
        raise HTTPException(404, 'People preview is not enabled')
    # Never accept a client-selected host/path outside this fixed pilot API.
    if not re.fullmatch(r'[a-zA-Z0-9/_-]*', path) or '..' in path:
        raise HTTPException(400, 'Invalid preview path')
    body = await request.body()
    limit = 1024 * 1024 if path in {'review-actions/stream','review/match','review/ignore','review/set-aside','other/match','other/ignore'} or re.fullmatch(r'groups/\d+/selected-faces',path) else 65536
    if len(body) > limit:
        raise HTTPException(413, 'Preview request too large')
    base = f"http://127.0.0.1:{cfg['port']}"
    headers = {'cookie': request.headers.get('cookie', ''), 'origin': base,
               'content-type': 'application/json'}
    if request.method=='POST' and (path in ('review-actions/stream','auto-organize/stream') or re.fullmatch(r'jobs/[a-z0-9-]+/recheck/stream',path)):
        return StreamingResponse(review_stream(base,headers,body,cfg['pilot'],path),
                                 media_type='application/x-ndjson',
                                 headers={'cache-control':'no-store','X-Accel-Buffering':'no'})
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
            response = await client.request(request.method, base+'/api/automatic-people'+('/'+path if path else ''),
                                            params=request.query_params, content=body, headers=headers)
    except httpx.TimeoutException:
        message = ('Detect again took longer than the response timeout. It may still be running or may have completed. Wait a moment, then refresh People to see the latest results before running it again.'
                   if request.method == 'POST' and path.endswith('/recheck') else
                   'People took too long to respond. Wait a moment, then refresh People. If you submitted a change, check the results before submitting it again.')
        raise HTTPException(504, message) from None
    except httpx.ConnectError:
        raise HTTPException(503, 'Cannot connect to the People service. It may be starting or temporarily unavailable. Try again shortly; if this continues, check FamilyFoto Server.') from None
    except httpx.HTTPError:
        raise HTTPException(502, 'The connection to the People service was interrupted. Refresh People to check the latest results before retrying an action.') from None
    if response.headers.get('x-familyfoto-pilot') != cfg['pilot']:
        raise HTTPException(503, 'People preview identity mismatch')
    return Response(response.content, status_code=response.status_code,
                    headers={'content-type':response.headers.get('content-type','application/json'),
                             'cache-control':'no-store'})


async def review_stream(base,headers,body,pilot,path='review-actions/stream'):
    try:
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=30) as client:
            async with client.stream('POST',base+'/api/automatic-people/'+path,
                                     content=body,headers=headers) as response:
                if response.headers.get('x-familyfoto-pilot')!=pilot:
                    yield json.dumps({'state':'failed','message':'People service identity mismatch; no completion confirmed.'})+'\n'
                    return
                if not response.is_success:
                    await response.aread()
                    yield json.dumps({'state':'failed','message':'People could not start the action. Refresh People and check the selection.',
                                      'status':response.status_code})+'\n'
                    return
                async for chunk in response.aiter_bytes():
                    yield chunk
    except httpx.HTTPError:
        yield json.dumps({'state':'failed','message':
            'Progress connection interrupted. The action may still be running or saved. Refresh People to check the results before trying again.'})+'\n'


def launch():
    """Only the independent desktop server calls this, never an HTTP request."""
    import subprocess
    import sys
    cfg = settings()
    if not cfg:
        return None
    stop = cfg['root']/'DESKTOP-PREVIEW-STOP'
    stop.unlink(missing_ok=True)
    with (cfg['root']/'desktop-server.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'face_detect.jobs.automatic_people_local',
            '--root', str(cfg['root']), '--port', str(cfg['port'])], cwd=ROOT,
            stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return process, stop
