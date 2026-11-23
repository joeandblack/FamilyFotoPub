"""Fixture-only preview router. Deliberately NOT included in app.api/app.service.server."""
from fastapi import APIRouter,Depends,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictInt,create_model
from typing import Literal
from app.database import connection
from app.auth.deps import reader
from app.auth.deps import administrator
from app.auth.deps import Identifier
from face_detect.recognition import automatic_people as service
from face_detect.recognition import people

router=APIRouter(prefix='/api/automatic-people',tags=['disabled-automatic-people'])
class Change(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=0)
    import_id:StrictInt|None=Field(default=None,gt=0)
    group_versions:dict[str,str]|None=Field(default=None,max_length=5000)
class Name(Change):name:str=Field(min_length=1,max_length=100)
class Rename(Name):expected_name:str|None=Field(default=None,min_length=1,max_length=100)
class Correct(Change):person_id:int=Field(gt=0)
class Cover(Change):
    detection_id:int=Field(gt=0)
    expected_cover_id:int|None=Field(default=None,gt=0)
class Move(Change):direction:Literal['up','down']
class Place(Change):
    target_id:StrictInt=Field(gt=0)
    after:StrictBool=False
    order_revision:StrictInt|None=Field(default=None,ge=0)
class Ignore(Change):enabled:StrictBool
class SelectedFaces(Change):
    detection_ids:list[StrictInt]=Field(min_length=1,max_length=5000)
    action:Literal['move','ignore','review-later']
    person_id:StrictInt|None=Field(default=None,gt=0)
class SetAside(Change):
    group_ids:list[StrictInt]=Field(min_length=1,max_length=5000)
    group_faces:dict[str,list[StrictInt]]|None=Field(default=None,max_length=5000)
class SuggestionDecision(Change):
    run_id:StrictInt=Field(gt=0)
    detection_id:StrictInt=Field(gt=0)
    person_id:StrictInt=Field(gt=0)
    decision:Literal['confirm','reject']
class MatchReview(SetAside):person_id:StrictInt=Field(gt=0)
class BulkReview(SetAside):
    action:Literal['match','ignore','set-aside']
    section:Literal['review','other']
    person_id:StrictInt|None=Field(default=None,gt=0)
def invoke(action,*args,revision=None,group_versions=None,group_ids=None,face_id=None,group_faces=None,progress=None,reconcile=False,import_id=None):
    try:
        service.guard()
        with connection() as db:
            from face_detect.recognition.people_review_scope import require_faces
            if face_id is not None:require_faces(db,import_id,[face_id])
            if action=='change_selected_faces':require_faces(db,import_id,args[1])
            if group_faces is not None:require_faces(db,import_id,{did for ids in group_faces.values() for did in ids})
            selected_groups=None
            if import_id is not None and action in ('set_aside_groups','match_review_groups','ignore_review_groups') and group_faces is None:
                raise ValueError('Filtered review requires a face selection snapshot')
            if reconcile and (group_faces is None or group_versions is None):
                raise ValueError('Reload People once to enable automatic selection updates')
            if group_faces is not None and (group_versions is None or group_ids is None or action not in ('set_aside_groups','match_review_groups','ignore_review_groups')):
                raise ValueError('Face selection requires selected group versions')
            if revision is not None:
                if progress:progress('waiting',0,None)
                people._lock(db)
                if progress:progress('checking',0,None)
                if group_versions is not None and face_id is not None:
                    group_ids=[g['id'] for g in service.catalog(db,import_id=import_id)['groups']
                               if any(f['detection_id']==face_id for f in g['faces'])]
                    if len(group_ids)!=1:raise RuntimeError('Selected face changed; refresh People')
                if group_versions is not None and group_ids is not None:
                    selected_groups=service.validate_review_groups(db,group_ids,group_versions,group_faces,reconcile=reconcile,import_id=import_id)
                elif people.revision(db)!=revision:raise RuntimeError('People changed; refresh')
            options={'selected_groups':selected_groups} if group_faces is not None else {}
            if action in ('catalog','detail','name_group','match_group','change_selected_faces'):
                options['import_id']=import_id
            if action=='change_selected_faces' and selected_groups is not None:
                options['validated_group']=selected_groups.get(args[0])
            if progress:options['progress']=progress
            if reconcile:
                active=[gid for gid in group_ids if gid in selected_groups]
                pid=args[1] if action=='match_review_groups' else None
                if pid is not None and not db.execute('SELECT 1 FROM people WHERE id=%s AND deleted_at IS NULL',(pid,)).fetchone():
                    active=[]
                result=getattr(service,action)(db,active,*args[1:],**options) if active else {}
                if progress:progress('updating',None,None)
                result['updates']=service.review_updates(db,group_ids,group_faces,pid,import_id=import_id)
                result['remaining_group_ids']=[gid for gid in group_ids if gid not in active]
            else:result=getattr(service,action)(db,*args,**options)
            if progress:progress('saving',None,None)
            return result
    except LookupError as error:raise HTTPException(404,str(error)) from error
    except ValueError as error:raise HTTPException(422,str(error)) from error
    except RuntimeError as error:raise HTTPException(409,str(error)) from error


@router.post('/review-actions/stream')
async def bulk_review(data:BulkReview,user=Depends(administrator)):
    from fastapi.responses import StreamingResponse
    from face_detect.recognition.people_review_progress import stream_review
    service.guard()
    if data.action=='match' and data.person_id is None:
        raise HTTPException(422,'Choose an existing person')
    if data.action=='set-aside' and data.section!='review':
        raise HTTPException(422,'Only To be reviewed groups can be moved to Review later')
    action={'match':'match_review_groups','ignore':'ignore_review_groups','set-aside':'set_aside_groups'}[data.action]
    args=[data.group_ids]
    if data.action=='match':args.append(data.person_id)
    args.append(user['username'])
    if data.action!='set-aside':args.append(data.section)
    def run(progress):
        return invoke(action,*args,revision=data.revision,group_versions=data.group_versions,
                      group_ids=data.group_ids,group_faces=data.group_faces,progress=progress,
                      import_id=data.import_id,
                      reconcile=data.group_faces is not None and data.group_versions is not None)
    return StreamingResponse(stream_review(run),media_type='application/x-ndjson',
                             headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

@router.get('')
def catalog(user=Depends(reader),import_id:int|None=Query(None,gt=0)):return invoke('catalog',import_id=import_id)
@router.get('/named-people')
def named_people(user=Depends(reader)):return invoke('named_people')
@router.get('/cards')
def named_cards(user=Depends(reader),import_id:int|None=Query(None,gt=0)):
    with connection() as db:return service.named_cards(db,import_id)

@router.get('/review-page')
def review_page(user=Depends(reader),section:Literal['review','other']='review',
                page:int=Query(1,ge=1),page_size:int=Query(100,ge=1,le=1000),
                import_id:int|None=Query(None,gt=0)):
    from face_detect.api.people_review_pages import review_page as read_page
    with connection() as db:return read_page(db,section,page,page_size,import_id)

@router.get('/review-imports')
def review_imports(user=Depends(reader)):
    from face_detect.recognition.people_review_scope import scanned_imports
    service.guard()
    with connection() as db:return scanned_imports(db)

class OrganizeFaces(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['run','undo']='run'
    event_id:StrictInt|None=Field(default=None,gt=0)
    import_id:StrictInt|None=Field(default=None,gt=0)


@router.get('/auto-organize')
def organize_status(user=Depends(administrator),import_id:int|None=Query(None,gt=0)):
    service.guard()
    with connection() as db:
        row=db.execute("SELECT id,undone,before_state->'summary' summary FROM auto_people_events WHERE action='auto-organize' AND (before_state->>'import_id') IS NOT DISTINCT FROM %s ORDER BY id DESC LIMIT 1",(str(import_id) if import_id else None,)).fetchone()
        return {'last_run':row}


@router.post('/auto-organize/stream')
def organize_faces(data:OrganizeFaces,user=Depends(administrator)):
    from fastapi.responses import StreamingResponse
    from face_detect.recognition.people_review_progress import stream_review
    from face_detect.recognition import people_auto_organize as organize
    from face_detect.jobs.automatic_people_jobs import manual_lease
    if data.action=='undo' and data.event_id is None:raise HTTPException(422,'Choose an Auto-organize run to undo')
    def run(progress):
        try:
            if data.action=='run':
                return organize.run(user['username'],progress,import_id=data.import_id,connect=connection)
            with manual_lease(progress):
                progress('saving',0,None)
                with connection() as db:return organize.undo(db,data.event_id,user['username'],import_id=data.import_id)
        except ValueError as error:raise HTTPException(422,str(error)) from error
        except RuntimeError as error:raise HTTPException(409,str(error)) from error
    return StreamingResponse(stream_review(run),media_type='application/x-ndjson',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
@router.get('/blurry')
def blurry_faces(page:int=Query(1,ge=1),user=Depends(reader)):
    from face_detect.recognition.people_face_quality import collection
    with connection() as db:return collection(db,page)
@router.post('/blurry/{did}/review')
def review_blurry_face(did:Identifier,user=Depends(administrator)):
    from face_detect.recognition.people_face_quality import release
    try:
        with connection() as db:return release(db,did,user['username'])
    except LookupError as error:raise HTTPException(404,str(error)) from error
    except ValueError as error:raise HTTPException(422,str(error)) from error
@router.get('/suggestions')
def suggestions(page:int=Query(1,ge=1),user=Depends(reader),import_id:int|None=Query(None,gt=0)):
    from face_detect.recognition.people_relaxed import collection
    service.guard()
    with connection() as db:return collection(db,page,import_id=import_id)
@router.post('/suggestions/decision')
def suggestion_decision(data:SuggestionDecision,user=Depends(administrator)):
    from face_detect.recognition.people_relaxed import decide
    try:
        service.guard()
        with connection() as db:
            people._lock(db)
            from face_detect.recognition.people_review_scope import require_faces
            require_faces(db,data.import_id,[data.detection_id])
            # decide validates the suggestion run and current face/person eligibility
            # under this lock; unrelated scan receipts must not invalidate a choice.
            return decide(db,data.run_id,data.detection_id,data.person_id,data.decision,user['username'])
    except ValueError as error:raise HTTPException(422,str(error)) from error
    except RuntimeError as error:raise HTTPException(409,str(error)) from error
@router.post('/review/set-aside')
def set_aside(data:SetAside,user=Depends(administrator)):
    return invoke('set_aside_groups',data.group_ids,user['username'],revision=data.revision,group_versions=data.group_versions,group_ids=data.group_ids,group_faces=data.group_faces,import_id=data.import_id)
@router.post('/review/match')
def match_review(data:MatchReview,user=Depends(administrator)):
    return invoke('match_review_groups',data.group_ids,data.person_id,user['username'],revision=data.revision,group_versions=data.group_versions,group_ids=data.group_ids,group_faces=data.group_faces,import_id=data.import_id)
@router.get('/ignored')
def ignored(page:int=Query(1,ge=1),user=Depends(reader)):
    return invoke('ignored_collection',page)
@router.post('/review/ignore')
def ignore_review(data:SetAside,user=Depends(administrator)):
    return invoke('ignore_review_groups',data.group_ids,user['username'],revision=data.revision,group_versions=data.group_versions,group_ids=data.group_ids,group_faces=data.group_faces,import_id=data.import_id)
@router.get('/groups/{gid}')
def detail(gid:Identifier,user=Depends(reader),import_id:int|None=Query(None,gt=0)):return invoke('detail',gid,import_id=import_id)
@router.post('/groups/{gid}/selected-faces')
def selected_faces(gid:Identifier,data:SelectedFaces,user=Depends(administrator)):
    return invoke('change_selected_faces',gid,data.detection_ids,data.action,data.person_id,user['username'],
                  revision=data.revision,group_versions=data.group_versions,group_ids=[gid],import_id=data.import_id)
@router.post('/other/match')
def match_other(data:MatchReview,user=Depends(administrator)):
    return invoke('match_review_groups',data.group_ids,data.person_id,user['username'],'other',revision=data.revision,group_versions=data.group_versions,group_ids=data.group_ids,group_faces=data.group_faces,import_id=data.import_id)
@router.post('/other/ignore')
def ignore_other(data:SetAside,user=Depends(administrator)):
    return invoke('ignore_review_groups',data.group_ids,user['username'],'other',revision=data.revision,group_versions=data.group_versions,group_ids=data.group_ids,group_faces=data.group_faces,import_id=data.import_id)
@router.post('/groups/{gid}/name')
def name(gid:Identifier,data:Name,user=Depends(administrator)):
    return invoke('name_group',gid,data.name,user['username'],revision=data.revision,group_versions=data.group_versions,group_ids=[gid],import_id=data.import_id)
@router.post('/faces/{did}/correct')
def correct(did:Identifier,data:Correct,user=Depends(administrator)):
    return invoke('correct',did,data.person_id,user['username'],revision=data.revision,group_versions=data.group_versions,face_id=did,import_id=data.import_id)
@router.post('/groups/{gid}/match')
def match_group(gid:Identifier,data:Correct,user=Depends(administrator)):
    return invoke('match_group',gid,data.person_id,user['username'],revision=data.revision,group_versions=data.group_versions,group_ids=[gid],import_id=data.import_id)
@router.post('/groups/{gid}/cover')
def cover(gid:Identifier,data:Cover,user=Depends(administrator)):
    return invoke('set_cover',gid,data.detection_id,user['username'],data.expected_cover_id,
                  revision=data.revision if data.expected_cover_id is None else None)
@router.post('/groups/{gid}/order')
def move_group(gid:Identifier,data:Move,user=Depends(administrator)):
    return invoke('move_group',gid,data.direction,user['username'],revision=data.revision)
@router.post('/groups/{gid}/position')
def place_group(gid:Identifier,data:Place,user=Depends(administrator)):
    return invoke('place_named_group',gid,data.target_id,data.after,user['username'],data.order_revision,
                  revision=data.revision if data.order_revision is None else None)
@router.post('/people/{pid}/rename')
def rename(pid:Identifier,data:Rename,user=Depends(administrator)):
    return invoke('rename_person',pid,data.name,user['username'],data.expected_name,
                  revision=data.revision if data.expected_name is None else None)
@router.post('/faces/{did}/ignored')
def ignore(did:Identifier,data:Ignore,user=Depends(administrator)):
    return invoke('ignore',did,data.enabled,user['username'],revision=data.revision,group_versions=data.group_versions,face_id=did,import_id=data.import_id)
@router.post('/events/{eid}/undo')
def undo(eid:Identifier,data:Change,user=Depends(administrator)):
    return invoke('undo_correction',eid,user['username'])


def job_router(jobs, resolve_inputs, dispatch, raw_media=False, server_selection=False, job_visible=None):
    """Unregistered fixture contract. Dispatch is supplied, never a new scheduler.

    A production launch integration must supply the existing explicit worker launch
    path. Requests select IDs only; prepared filesystem paths are resolved internally.
    """
    result = APIRouter(prefix='/api/automatic-people/jobs')
    class StartBase(BaseModel):
        model_config = ConfigDict(extra='forbid')
        id: str = Field(pattern=r'^[a-z0-9-]{1,64}$')
    selection_field='selection' if server_selection else 'media_ids' if raw_media else 'detection_ids'
    StartJob=create_model('StartRawJob' if raw_media else 'StartDetectionJob',__base__=StartBase,
        **{selection_field:(str,Field(min_length=1,max_length=40)) if server_selection else (list[StrictInt],Field(min_length=1,max_length=10 if raw_media else 100))})
    class ControlJob(BaseModel):
        model_config = ConfigDict(extra='forbid')
        action: Literal['pause','cancel','resume']
    def call(fn, *args, **kwargs):
        try: return fn(*args, **kwargs)
        except FileNotFoundError as error: raise HTTPException(404,'Job not found') from error
        except FileExistsError as error: raise HTTPException(409,'Job already exists') from error
        except PermissionError as error: raise HTTPException(403,str(error)) from error
        except ValueError as error: raise HTTPException(422,str(error)) from error
        except RuntimeError as error: raise HTTPException(409,str(error)) from error
    @result.post('')
    def start(data:StartJob,user=Depends(administrator)):
        call(service.guard)
        selected_ids=getattr(data,selection_field)
        if server_selection and getattr(jobs,'import_scoped',False):
            if not selected_ids.startswith('import:') or data.id!='import-'+selected_ids.removeprefix('import:'):
                raise HTTPException(422,'Job must match the selected import')
        if not server_selection and (any(i<=0 for i in selected_ids) or len(set(selected_ids))!=len(selected_ids)):
            raise HTTPException(422,'Unique positive detection IDs required')
        call(jobs.create,data.id,call(resolve_inputs,selected_ids),user)
        if call(dispatch,data.id,user) is False:
            raise HTTPException(409,'Worker still active; retry Resume after it yields')
        return call(jobs.status,data.id,user)
    @result.get('/{name}')
    def status(name:str,user=Depends(reader)):
        # A hidden job answers exactly like one that does not exist.
        if job_visible is not None and not job_visible(name):raise HTTPException(404,'Job not found')
        return call(jobs.status,name,user)
    @result.post('/{name}/recheck')
    def recheck(name:str,data:Change,user=Depends(administrator)):
        if not hasattr(jobs,'recheck'):raise HTTPException(409,'Import-scoped discovery required')
        return call(jobs.recheck,name,data.revision,user,**({'import_id':data.import_id} if data.import_id else {}))

    @result.post('/{name}/recheck/stream')
    async def recheck_stream(name:str,data:Change,user=Depends(administrator)):
        from fastapi.responses import StreamingResponse
        from face_detect.recognition.people_review_progress import stream_review
        if not hasattr(jobs,'recheck'):raise HTTPException(409,'Import-scoped discovery required')
        def run(progress):
            return call(jobs.recheck,name,data.revision,user,progress=progress,**({'import_id':data.import_id} if data.import_id else {}))
        return StreamingResponse(stream_review(run),media_type='application/x-ndjson',
                                 headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
    @result.post('/{name}/control')
    def control(name:str,data:ControlJob,user=Depends(administrator)):
        state=call(jobs.control,name,data.action,user)
        if data.action=='resume':
            if call(dispatch,name,user) is False:
                raise HTTPException(409,'Worker still active; retry Resume after it yields')
            state=call(jobs.status,name,user)
        return state
    return result
