"""Sign-in, sessions, and the request gate that protects every route."""
import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from ..config import ROOT, load_config
from ..service.instance import session_cookie
from ..telemetry import logger
from . import (session_user, accounts, authenticate,
                   revoke_session, revoke_sessions, SESSION_SECONDS, token_digest,
                   reserve_password_attempt, verify_password)
from .store import SessionStore
from . import manage
from ..jobs.job_access import job_in_path, can_access
from ..viewer import for_user, set_current as set_viewer, reset as reset_viewer

log=logger('web')
router=APIRouter()

async def protect(request:Request,call_next):
    config=load_config()
    try:
        verified_user=await run_in_threadpool(session_user,config,request.cookies.get(session_cookie(config),''))
    except (DatabaseError,PoolTimeout,ValueError):
        return JSONResponse({'error':'Sign-in service is temporarily unavailable. Try again shortly.'},503)
    user=verified_user
    public=request.url.path in ('/login','/api/login') or request.url.path.startswith('/web/')
    if not public and not user:
        if request.url.path in ('/','/people-preview'):
            return RedirectResponse('/login',status_code=303)
        return JSONResponse({'error':'Login required'},401)
    request.state.user=user
    request.state.authenticated = verified_user is not None
    if request.method not in ('GET','HEAD','OPTIONS') and request.url.path not in ('/api/login','/api/auth/logout','/api/auth/logout-all','/api/auth/activity'):
        if not user or user['role']=='guest':
            return JSONResponse({'detail':'Read-only account: changes are not allowed'},403)
        if request.url.path.rstrip('/').endswith('/trash/delete') and user['role']!='admin':
            return JSONResponse({'detail':'Only an administrator can permanently delete files'},403)
        if (request.url.path in ('/api/storage/settings','/api/upload-settings','/api/storage/select-folder','/api/import/select-folder','/api/import/start') or request.url.path.endswith('/import/resume')) and user['role']!='admin':
            return JSONResponse({'detail':'Only an administrator can select storage or import server folders'},403)
    if request.method!='GET':
        origin=request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'error':'Cross-origin commands are not allowed'},403)
    job_id=job_in_path(request.url.path) if user else None
    if job_id is not None and not await run_in_threadpool(can_access,user,job_id):
        return JSONResponse({'detail':'Import not found'},404)
    viewer_token=set_viewer(for_user(user))
    try:
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Cache-Control']='no-store'
        return response
    except Exception:
        log.exception('request-failed path=%s',request.url.path)
        return JSONResponse({'error':'Request failed; see local web log'},500)
    finally:
        reset_viewer(viewer_token)

@router.get('/login')
def login_page():
    return FileResponse(ROOT/'web'/'login.html')

@router.post('/api/login')
async def login(request:Request):
    config=load_config()
    data=await small_json(request)
    username,password=data.get('username'),data.get('password')
    if (not isinstance(username,str) or not 1<=len(username)<=128 or
            not isinstance(password,str) or not 1<=len(password)<=1024):
        return JSONResponse({'error':'Enter a username and password within the allowed length.'},422)
    try:
        token,retry=await run_in_threadpool(authenticate,config,username,password,
                                          request.client.host if request.client else 'unknown')
    except (DatabaseError,PoolTimeout,ValueError):
        return JSONResponse({'error':'Sign-in service is temporarily unavailable. Try again shortly.'},503)
    if retry:
        return JSONResponse({'error':f'Too many sign-in attempts. Try again in {retry} seconds.',
                             'retry_after':retry},429,headers={'Retry-After':str(retry)})
    if not token:
        return JSONResponse({'error':'Incorrect username or password'},401)
    # Replacing a browser's cookie also invalidates its previous server session.
    await run_in_threadpool(revoke_session,config,request.cookies.get(session_cookie(config),''))
    response=JSONResponse({'ok':True})
    response.set_cookie(session_cookie(config),token,max_age=SESSION_SECONDS,
                        httponly=True,samesite='strict',secure=request.url.scheme=='https')
    return response

@router.post('/api/auth/activity')
def session_activity(request:Request):
    config=load_config()
    token=request.cookies.get(session_cookie(config),'')
    row=SessionStore(config).activity(token_digest(token))
    if not row:
        raise HTTPException(401,'Login required')
    response=JSONResponse({'ok':True})
    response.set_cookie(session_cookie(config),token,max_age=row['remaining'],
                        httponly=True,samesite='strict',secure=request.url.scheme=='https')
    return response

async def small_json(request):
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>8192:
            raise HTTPException(413,'Request too large')
        body.extend(chunk)
    try:
        data=json.loads(body)
    except (ValueError,UnicodeError):
        raise HTTPException(422,'JSON object required') from None
    if not isinstance(data,dict):
        raise HTTPException(422,'JSON object required')
    return data

def clear_session_response(request, **values):
    response=JSONResponse({'ok':True,**values})
    response.delete_cookie(session_cookie(load_config()),httponly=True,samesite='strict',secure=request.url.scheme=='https')
    return response

@router.post('/api/auth/logout')
def logout(request:Request):
    config=load_config()
    revoke_session(config,request.cookies.get(session_cookie(config),''))
    return clear_session_response(request)


@router.post('/api/auth/logout-all')
def logout_all(request:Request):
    if not request.state.authenticated:
        raise HTTPException(401,'Login required')
    count=revoke_sessions(load_config(),request.state.user['username'])
    return clear_session_response(request,revoked=count)


@router.get('/api/auth/accounts')
def account_names(request:Request):
    if not request.state.authenticated or request.state.user['role']!='admin':
        raise HTTPException(403,'Administrator required')
    return {'accounts':list(accounts(load_config()))}


@router.post('/api/auth/revoke')
async def revoke_account(request:Request):
    if not request.state.authenticated or request.state.user['role']!='admin':
        raise HTTPException(403,'Administrator required')
    data=await small_json(request)
    username=data.get('username')
    config=load_config()
    if not isinstance(username,str) or username not in accounts(config):
        raise HTTPException(422,'Choose an existing account')
    count=await run_in_threadpool(revoke_sessions,config,username)
    if username==request.state.user['username']:
        return clear_session_response(request,revoked=count)
    return {'ok':True,'revoked':count}


def require_admin(request):
    if not request.state.authenticated or request.state.user['role']!='admin':
        raise HTTPException(403,'Administrator required')


async def change_accounts(request, data, change):
    password = data.get('admin_password')
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise HTTPException(403, 'Enter your current administrator password')
    username = request.state.user['username']
    address = request.client.host if request.client else 'unknown'

    def confirmed_change(config):
        # Verify the signed-in administrator against the current settings while the
        # shared write lock is held, so a concurrent reset cannot bypass confirmation.
        retry = reserve_password_attempt(config, username, address)
        if retry:
            raise HTTPException(429, 'Too many password attempts. Try again shortly.',
                                headers={'Retry-After': str(retry)})
        account = accounts(config).get(username)
        if not account or account['role'] != 'admin' or not verify_password(account, password):
            raise HTTPException(403, 'Administrator password is incorrect')
        return change(config)

    try:
        return await run_in_threadpool(manage.update_config,confirmed_change)
    except ValueError as error:
        raise HTTPException(422,str(error)) from None
    except RuntimeError as error:
        raise HTTPException(409,str(error)) from None


@router.get('/api/auth/users')
def user_list(request:Request):
    require_admin(request)
    return {'users':manage.account_list(load_config())}


@router.post('/api/auth/users')
async def create_user(request:Request):
    require_admin(request)
    data=await small_json(request)
    username,password,role=data.get('username'),data.get('password'),data.get('role')
    updated=await change_accounts(request,data,lambda config:manage.with_new_account(config,username,password,role))
    log.info('Administrator %s created account %s (%s)',request.state.user['username'],username,role)
    return {'ok':True,'users':manage.account_list(updated)}


@router.post('/api/auth/users/password')
async def reset_user_password(request:Request):
    require_admin(request)
    data=await small_json(request)
    username,password=data.get('username'),data.get('password')
    updated=await change_accounts(request,data,lambda config:manage.with_new_password(config,username,password))
    # The new hash already invalidates old sessions; revoking also clears their records.
    count=await run_in_threadpool(revoke_sessions,updated,username)
    log.info('Administrator %s reset the password for %s',request.state.user['username'],username)
    if username==request.state.user['username']:
        return clear_session_response(request,revoked=count,signed_out=True)
    return {'ok':True,'revoked':count,'signed_out':False}

@router.get('/api/me')
def me(request:Request):
    user = request.state.user
    from face_detect.api.people_preview_gateway import settings as preview_settings
    available = getattr(request.app.state, 'existing_people_catalog', False) or preview_settings() is not None
    return {**user, 'capabilities': {'people_preview': request.state.authenticated and available}}
