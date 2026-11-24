"""Admin storage settings; source identity is pinned separately for each import."""
import copy
import ctypes
import hashlib
import json
import os
import sys
from pathlib import Path, PureWindowsPath
import tempfile
from psycopg.types.json import Jsonb
from ..config import ROOT,configuration_root,load_config
from ..config_lock import config_write_lock
from .disks import snapshot
from ..imports.manual import source_folder

CONFIG_PATH=configuration_root()/'local.json'


def storage_drive(path):
    if sys.platform == 'darwin':
        from .macos_disks import drive_for_path
        return drive_for_path(path)
    return Path(path).drive.rstrip(':')


def same_legacy_volume(path, identity):
    if sys.platform == 'darwin':
        # Mac jobs always pin source_storage; never infer a UUID from a path.
        return False
    def drive(value):
        return (PureWindowsPath(value).drive or Path(value).drive).casefold()
    return drive(path) == drive(identity.get('root', ''))



def enroll_source(config,value):
    path=source_folder(config,value)
    if str(path).startswith('\\\\') or (os.name=='nt' and ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))==4):
        # Existing Windows account access only; never collect share credentials.
        return {'root':str(path),'kind':'network','drive':None}
    drive=storage_drive(path)
    disk=snapshot([drive])[drive]
    if not disk['online']:raise ValueError('Source disk is offline')
    return {'root':str(path),'drive':drive,**{k:disk[k] for k in ('serial','volume_serial','volume_id')}}


def import_config(config,job):
    result=copy.deepcopy(config)
    if job.get('source_kind')=='upload':
        result['source']={'kind':'upload','root':job['source_root']}
        return result
    identity=job.get('source_storage')
    if not identity:
        # Legacy imports retain the previously enrolled volume, never silently
        # enroll whatever disk happens to be mounted at that letter now.
        path=Path(job['source_root'])
        if not same_legacy_volume(path, config['source']):
            raise ValueError('Legacy import source identity needs review')
        identity={**config['source'],'root':job['source_root']}
    result['source']={**identity,'root':job['source_root']}
    source_folder(result,job['source_root'])
    return result


def revision():
    return hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()


def latest_source(db,config):
    """Read-only display identity; an offline source remains the latest source."""
    job=db.execute("SELECT source_root,source_storage FROM jobs WHERE state<>'cancelled' AND source_kind='server' ORDER BY id DESC LIMIT 1").fetchone()
    if not job:return dict(config['source'])
    identity=job.get('source_storage')
    if not identity:
        if not config['source'].get('root') or not same_legacy_volume(job['source_root'], config['source']):
            return {'root':job['source_root'],'kind':'unverified'}
        identity=config['source']
    return {**identity,'root':job['source_root']}


def settings(db):
    from ..jobs import snapshot as pipeline_snapshot
    config=load_config()
    populated=db.execute("SELECT EXISTS(SELECT 1 FROM media) OR EXISTS(SELECT 1 FROM jobs) OR EXISTS(SELECT 1 FROM upload_sessions WHERE state NOT IN ('cancelled','done')) populated").fetchone()['populated']
    return {'source':config['source'].get('root',''),'latest_source':latest_source(db,config).get('root',''),'destination':config['destination'].get('root',''),
            'destination_locked':populated,'busy':pipeline_snapshot(db)['busy'],'revision':revision()}


def marker_refusal(destination):
    """Refuse a destination whose parent folder already holds a library's volume marker.

    The marker sits beside a library's media folder, so a second library there would be
    mistaken for the first. The refusal names the marker and the library it belongs to,
    and says when that library's folder is gone, so a leftover can be recognised. A
    leftover is never ignored or removed here: whether that library is really gone is
    the owner's call."""
    marker=destination.parent/'.familyfoto-volume.json'
    if not marker.exists():
        return
    other=''
    try:
        with marker.open('rb') as stream:
            other=json.loads(stream.read(65536)).get('root') or ''
    except (OSError,ValueError,AttributeError):
        pass
    other=other if isinstance(other,str) else ''
    elsewhere=f'Choose a folder inside a new empty folder instead, for example {destination/"media"}.'
    if not other:
        raise ValueError(f'{marker} marks this folder as holding a FamilyFoto library. {elsewhere}')
    if Path(other).exists():
        raise ValueError(f'{marker} belongs to the FamilyFoto library at {other}. {elsewhere}')
    raise ValueError(f'{marker} was left by a FamilyFoto library at {other}, which no longer exists. '
                     f'If that library is gone, delete the marker and try again. Or: {elsewhere}')


def save(db,payload):
    from ..jobs import idle
    idle(db)  # Serialize configuration with imports, maintenance and other saves.
    if set(payload)!={'source','destination','revision'} or not all(isinstance(v,str) for v in payload.values()):
        raise ValueError('Source, destination and configuration revision are required')
    if payload['revision']!=revision():raise ValueError('Settings changed; refresh Settings before saving')
    config=load_config();updated=copy.deepcopy(config)
    old_destination=Path(config['destination']['root']).resolve() if config['destination'].get('root') else None
    candidate=Path(payload['destination'])
    if not candidate.is_absolute() or '..' in candidate.parts:raise ValueError('Destination must be an absolute server folder')
    destination=candidate.resolve()
    if destination!=old_destination:
        application=ROOT.resolve()
        if destination==application or application in destination.parents or destination in application.parents:
            raise ValueError('Library destination must be separate from the application folder')
        if settings(db)['destination_locked']:
            raise ValueError('This library already has imports. Moving its destination requires a separate verified migration; no settings changed')
        if str(candidate).startswith('\\\\'):raise ValueError('Destination requires a local disk with a verifiable volume identity')
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (candidate,*candidate.parents)):
            raise ValueError('Linked destination folders are not allowed')
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):raise ValueError('Choose a new or empty destination folder')
        marker_refusal(destination)
        drive=storage_drive(destination);disk=snapshot([drive])[drive]
        if sys.platform == 'darwin' and not disk.get('writable'):
            raise ValueError('Destination volume is read-only')
        if not disk['online']:raise ValueError('Destination disk is offline')
        updated['destination']={'root':str(destination),'drive':drive,**{k:disk[k] for k in ('serial','volume_serial','volume_id')}}
    # An unchanged disconnected default source need not be online to save.
    if payload['source']!=config['source'].get('root',''):
        updated['source']=enroll_source(updated,payload['source']) if payload['source'].strip() else {}
    if updated['source'].get('root'):
        source=Path(updated['source']['root']).resolve()
        if source==destination or source in destination.parents or destination in source.parents:
            raise ValueError('Source and destination must be separate trees')
    # Preserve legacy import identities before changing the default source.
    for job in db.execute("SELECT id,source_root FROM jobs WHERE source_storage IS NULL AND source_kind='server'").fetchall():
        if not config['source'].get('root') or not same_legacy_volume(job['source_root'], config['source']):
            raise ValueError('An older import needs source identity review before changing settings')
        identity={**config['source'],'root':job['source_root']}
        db.execute('UPDATE jobs SET source_storage=%s WHERE id=%s',(Jsonb(identity),job['id']))
    db.commit()  # Safe even if file replacement fails: identity is unchanged.
    # The shared lock keeps an account change from landing between this check and the write.
    with config_write_lock(CONFIG_PATH):
        if payload['revision']!=revision():raise ValueError('Settings changed; refresh Settings before saving')
        temp=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=CONFIG_PATH.parent,prefix='.storage-',delete=False) as f:
                temp=Path(f.name);json.dump(updated,f,indent=2);f.flush();os.fsync(f.fileno())
            os.replace(temp,CONFIG_PATH)
        finally:
            if temp and temp.exists():temp.unlink()
    return {'message':'Settings saved. Existing imports keep their source identities.'}
