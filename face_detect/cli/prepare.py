"""Explicitly prepare the People catalog for this computer's face model. Never scans media.

A library has one People catalog, the schema `people`. A fresh install creates it. An
install on a library that already has one (a Windows library restored on a Mac, say)
adopts it and never makes a second, provided this computer's model is the same space.
The profile folder under data/face_detect/profiles/<token> holds this computer's jobs and
calibration runs; its runtime.json points at the catalog.
"""
import json
from app import database
from app.config import DATA,load_config
from app.telemetry import write_json
from face_detect import catalog
from face_detect.configuration import resolve
from face_detect.service import create_service

def service_port(requested=None):
    path=DATA/'people-preview.json'
    existing=json.loads(path.read_text()) if path.exists() else {}
    port=requested if requested is not None else existing.get('port',54341)
    if type(port) is not int or not 49152<=port<=65535:
        raise ValueError('People port must be between 49152 and 65535')
    if existing and requested is not None and port!=existing.get('port'):
        raise ValueError('Stop and reconfigure the existing service before changing its port')
    return port


def prepare(port=None):
    port=service_port(port)
    cfg=load_config();_,profile=resolve(cfg);provider=create_service(cfg)
    token=provider.identity[:32]
    provider.close()
    if provider.space_id is None:raise ValueError('This face model defines no embedding space')
    root=DATA/'face_detect/profiles'/token
    root.mkdir(parents=True,exist_ok=True)
    with database.connection(cfg['database']) as db:
        db.execute('SELECT pg_advisory_xact_lock(4340051)')
        adopted=catalog.exists(db)
        if not adopted:
            # A library that still has only per-model schemas must be migrated, never given a
            # second, empty catalog beside them.
            if any(name!=catalog.SCHEMA for name in catalog.people_schemas(db)):
                raise ValueError('This library has People data from before the one catalog; run the catalog migration first')
            catalog.create(db)
        else:
            from face_detect.catalog.spaces import active_space,register_space
            db.execute("SELECT set_config('search_path', %s, true)",(catalog.search_path(),))
            active=active_space(db)
            if active is not None and active!=provider.space_id:
                raise ValueError('This library\'s People catalog uses another face model; install that model, or switch models separately')
            if active is None:
                # A catalog with no active model has no faces yet: a library backed up before
                # its first scan. Record this computer's model now, as that scan would before
                # writing. Until then a recovered library can neither start a scan nor edit People.
                register_space(db,provider)
    value={'schema':catalog.SCHEMA,'profile':profile,'model_id':provider.identity,'space_id':provider.space_id,
           'media_ids':[],'review_imports':[]}
    path=root/'runtime.json'
    if path.exists():
        existing=json.loads(path.read_text())
        if existing.get('schema')==catalog.SCHEMA and existing.get('space_id') not in (None,provider.space_id):
            raise ValueError('Catalog model mismatch')
        # Same space, or a profile from before the one catalog: point it at the catalog, keeping
        # its jobs and calibration settings.
        write_json(path,{**existing,**{k:value[k] for k in ('schema','profile','model_id','space_id')}})
    else:write_json(path,value)
    write_json(DATA/'people-preview.json',{'enabled':True,'backend':'native','pilot':token,'port':port})
    return {'root':str(root),'model_id':provider.identity,'space_id':provider.space_id,
            'provider':profile['provider'],'catalog':'adopted' if adopted else 'created'}

if __name__=='__main__':print(json.dumps(prepare()))
