"""Matching policy of a space; historical thresholds never reach a native space.

Only the catalog's active space matches. A retired space (the October CompreFace vectors)
is historical-only. Whether this computer may write vectors at all is decided separately,
by its certificate for the space.
"""
def matching_policy(db,model):
    if inactive_model(db,model):return {'calibrated':False,'mode':'historical-only'}
    row=db.execute('SELECT manifest FROM space WHERE space_id=%s',(model,)).fetchone()
    if not row:raise ValueError('Unknown model space')
    manifest=row['manifest']
    return (native_policy(db,model) or manifest.get('matching',{})) if manifest.get('schema')=='familyfoto-native-model-v1' else {'threshold':.4,'margin':.05,'calibrated':True}

def legacy_model(db,model):
    if inactive_model(db,model):return False
    row=db.execute('SELECT manifest FROM space WHERE space_id=%s',(model,)).fetchone()
    return bool(row and row['manifest'].get('schema')!='familyfoto-native-model-v1')


def inactive_model(db,model):
    from face_detect.catalog.spaces import active_space
    active=active_space(db)
    return active is not None and active!=model


def native_policy(db,model):
    row=db.execute('SELECT policy FROM policy WHERE space_id=%s',(model,)).fetchone()
    return row['policy'] if row else {}


def manual_policy(db, model):
    """Explicit trial settings never qualify background discovery matching."""
    policy = matching_policy(db, model)
    trial = policy.get('manual_trial', {})
    if trial.get('enabled') is True:
        return {**trial, 'manual_enabled': True, 'calibrated': False}
    return policy


def manual_enabled(policy):
    return policy.get('calibrated') is True or policy.get('manual_enabled') is True
