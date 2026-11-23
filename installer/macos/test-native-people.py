"""Explicit empty-install verification and sample inference; never uses personal media."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer.macos import people, manage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Allow a temporary private database and synthetic/sample fixtures')
    if not parser.parse_args().run:
        parser.error('Explicit --run required')
    os.umask(0o077)
    cfg = people.local_configuration(ROOT, sys.prefix)
    result = {'passed': False, 'scope':'empty-install verification and sample inference'}
    with people.installation_lock(ROOT):
        _, binary = manage.settings()
        with people.database_window(cfg, binary):
            result['catalog'] = people.verify(ROOT, cfg)
            subprocess.run([sys.executable, str(ROOT / 'installer/macos/people.py'), 'verify'],check=True,cwd=ROOT)
        with tempfile.TemporaryDirectory(prefix='native-smoke-',dir=ROOT/'temp') as directory:
            blank, astronaut = Path(directory)/'blank.png', Path(directory)/'astronaut.png'
            from PIL import Image
            Image.new('RGB',(320,240),(0,0,0)).save(blank)
            from face_detect.configuration import resolve
            _, profile = resolve(cfg)
            subprocess.run([profile['python'], '-c',
                            'from skimage import data; from PIL import Image; import sys; Image.fromarray(data.astronaut()).save(sys.argv[1])',
                            str(astronaut)],check=True,cwd=ROOT)
            for path, count in ((blank,0),(astronaut,1)):
                started = time.monotonic()
                completed = subprocess.run([sys.executable,'-m','face_detect.cli.doctor','--image',str(path)],
                                           capture_output=True,text=True,check=True,cwd=ROOT,timeout=180)
                observed = json.loads(completed.stdout)
                if observed['faces'] != count or not observed['unit_vectors'] or observed['dimensions'] != [512]*count:
                    raise ValueError('Unexpected fixture inference result')
                result[path.stem] = {'faces':count,'dimensions':observed['dimensions'],
                                     'unit_vectors':observed['unit_vectors'],'seconds':round(time.monotonic()-started,2)}
    result['passed'] = True
    (ROOT/'logs/native-people-smoke.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('Smoke test failed ('+type(error).__name__+'); retain this isolated installation for diagnosis.',file=sys.stderr)
        raise SystemExit(1)
