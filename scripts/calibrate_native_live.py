"""Local Windows calibration; no scans or existing-group edits are started."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
cfg = json.loads((ROOT/'config/local.json').read_text(encoding='utf-8'))
if not cfg.get('live_native_catalog'):
    raise ValueError('Native deployment required')
os.environ['FAMILYFOTO_DATA_DIR'] = cfg['preview']['data_root']

from face_detect.cli.calibrate import collect, evaluate, activate, save_equivalences
from app.telemetry import write_json

parser = argparse.ArgumentParser()
parser.add_argument('--collect-only', action='store_true')
parser.add_argument('--activate', action='store_true')
parser.add_argument('--equivalent-person-ids', nargs='+', type=int, action='append')
args = parser.parse_args()
if args.equivalent_person_ids:
    save_equivalences(args.equivalent_person_ids)
if args.collect_only:
    root, rows, revision = collect()
    target = root/'calibration'
    target.mkdir(exist_ok=True)
    write_json(target/'labeled-inputs.json', rows)
    print(json.dumps({'labels': len(rows), 'people': len({r['person_id'] for r in rows}),
                      'dates': len({r['event_id'] for r in rows}),
                      'quality_seeds': sum(r['seed'] for r in rows), 'revision': revision}))
else:
    report = evaluate()
    print(json.dumps(report), flush=True)
    if args.activate:
        print(json.dumps({'activation': activate(report)}), flush=True)
