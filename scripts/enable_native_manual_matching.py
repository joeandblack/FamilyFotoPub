"""Enable an explicitly requested manual trial without running any recognition job."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
cfg = json.loads((ROOT / 'config/local.json').read_text(encoding='utf-8'))
if not cfg.get('live_native_catalog'):
    raise ValueError('Preserved native deployment required')
os.environ['FAMILYFOTO_DATA_DIR'] = cfg['preview']['data_root']

import hashlib

from app import database
from face_detect import catalog
from face_detect.catalog.spaces import active_space
from psycopg.types.json import Jsonb


def main():
    with database.connection(catalog.database(cfg)) as db:
        model = active_space(db)
        manifest = db.execute('SELECT manifest FROM space WHERE space_id=%s', (model,)).fetchone()['manifest']
        if manifest.get('schema') != 'familyfoto-native-model-v1':
            raise ValueError('Native model required')
        saved = db.execute('SELECT policy,evidence FROM policy WHERE space_id=%s FOR UPDATE', (model,)).fetchone()
        policy = dict(saved['policy'] if saved else manifest.get('matching', {}))
        # Trial values are intentionally separate from qualified discovery policy.
        trial = {'enabled': True, 'threshold': .65, 'margin': .20,
                 'suggestion_threshold': .50}
        policy['manual_trial'] = trial
        evidence = dict(saved['evidence'] if saved else {})
        evidence['manual_trial'] = {'reason': 'User requested manual Windows testing',
                                    'validated': False, 'previous_policy': saved['policy'] if saved else None}
        policy_id = hashlib.sha256((model + ':' + json.dumps(policy, sort_keys=True)).encode()).hexdigest()
        db.execute('''INSERT INTO policy(policy_id,space_id,policy,evidence)
            VALUES(%s,%s,%s,%s) ON CONFLICT(space_id) DO UPDATE SET policy_id=EXCLUDED.policy_id,
            policy=EXCLUDED.policy,evidence=EXCLUDED.evidence,activated_at=now()''',
            (policy_id, model, Jsonb(policy), Jsonb(evidence)))
    print(json.dumps({'manual_trial': trial, 'discovery_matching_calibrated': policy.get('calibrated', False),
                      'jobs_started': 0, 'people_changed': 0}))


if __name__ == '__main__':
    main()
