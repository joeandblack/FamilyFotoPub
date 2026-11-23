"""Explicit isolated native-install acceptance test; no live configuration copied."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--postgres-bin', type=Path, required=True)
    parser.add_argument('--model-cache', type=Path)
    parser.add_argument('--research-only', action='store_true')
    args = parser.parse_args()
    if not args.research_only:
        parser.error('Explicit --research-only model terms approval required')
    package = args.package.resolve()
    if not package.is_relative_to(REPO / 'temp/installer-sandbox'):
        parser.error('Use a sanitized package from prepare-sandbox-test.ps1')
    if any((package / p).exists() for p in ('config/local.json', 'data', 'runtime', '.venv')):
        parser.error('Package contains private installation state')
    for path in package.rglob('*'):
        if path.is_symlink() or path.is_junction():
            parser.error('Linked package files are refused')
    for port in (55481, 55482, 55483):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    root = REPO.parent / ('FamilyFoto-native-install-test-' + uuid.uuid4().hex[:8])
    shutil.copytree(package, root)
    env = dict(os.environ)
    env.pop('FAMILYFOTO_DATA_DIR', None)
    env.pop('PYTHONPATH', None)
    python = root / '.venv/Scripts/python.exe'
    cluster = root / 'data/postgres'
    pgctl = args.postgres_bin.resolve() / 'pg_ctl.exe'
    started = False
    result = {'root': str(root), 'passed': False, 'phase': 'environment',
              'scope': 'fresh native People with reused PostgreSQL binaries; not full Core/VM qualification'}
    log = None

    def run(command, **kwargs):
        return subprocess.run([str(v) for v in command], cwd=root, env=env, check=True,
                              stdout=log, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)

    try:
        (root / 'logs').mkdir()
        log = (root / 'logs/native-install-test.log').open('w', encoding='utf-8')
        print('Isolated test directory: ' + str(root), flush=True)
        run([sys.executable, '-m', 'venv', root / '.venv'])
        run([python, '-m', 'pip', 'install', '-r', root / 'requirements.txt'])
        run([python, root / 'installer/configure.py', '--password-stdin',
             '--database-port', '55481', '--web-port', '55482'],
            input=secrets.token_urlsafe(32) + '\n', text=True)
        cfg = json.loads((root / 'config/local.json').read_text())
        # Restrict credentials before writing the temporary initdb password file.
        run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
             ". '" + str(root / 'installer/windows/setup-common.ps1') + "'; "
             "Set-SetupPrivateDirectory '" + str(root / 'config') + "'; "
             "Set-SetupPrivateDirectory '" + str(root / 'data') + "'"])
        password = root / 'data/.initdb-password'
        password.write_text(cfg['postgres_admin_password'], encoding='ascii')
        result['phase'] = 'database'
        try:
            run([args.postgres_bin / 'initdb.exe', '-D', cluster, '-U', 'postgres',
                 '--auth=scram-sha-256', '--encoding=UTF8', '--locale=C', '--pwfile=' + str(password)])
        finally:
            password.unlink(missing_ok=True)
        run([pgctl, '-D', cluster, '-l', root / 'logs/postgres.log',
             '-o', '-h 127.0.0.1 -p 55481', '-w', 'start'])
        started = True
        run([python, root / 'installer/init-database.py'])
        run([python, '-m', 'app.database.schema_upgrading'])
        if args.model_cache:
            # Copy only the official archive, never a live manifest, policy or profile.
            cache = root / 'runtime/face_detect/models/buffalo_l/pack.zip'
            cache.parent.mkdir(parents=True)
            shutil.copyfile(args.model_cache, cache)
        result['phase'] = 'native-install'
        run([python, root / 'installer/windows/people.py', '--mode', 'install',
             '--research-only', '--port', '55483'])
        result['phase'] = 'verify'
        run([python, root / 'installer/windows/people.py', '--mode', 'verify'])
        result['phase'] = 'blank-inference'
        run([python, '-c', "from PIL import Image; Image.new('RGB',(320,240),(0,0,0)).save('temp/blank.png')"])
        provider_python = root / 'runtime/face_detect/envs/insightface/Scripts/python.exe'
        run([provider_python, '-c', "from skimage import data; from PIL import Image; Image.fromarray(data.astronaut()).save('temp/astronaut.png')"])
        for name, count in (('blank', 0), ('astronaut', 1)):
            result['phase'] = name + '-inference'
            checked = subprocess.run([str(python), '-m', 'face_detect.cli.doctor',
                                      '--image', str(root / ('temp/' + name + '.png'))],
                                     cwd=root, env=env, capture_output=True, text=True,
                                     check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            observed = json.loads(checked.stdout)
            if observed['faces'] != count or not observed['unit_vectors'] or observed['dimensions'] != [512] * count:
                raise RuntimeError('Unexpected fixture inference result')
            result[name] = observed
        result.update(passed=True, phase='complete')
    except Exception as error:
        result['error_type'] = type(error).__name__
        print('Test failed during ' + result['phase'] + '; inspect its local log.', flush=True)
    finally:
        if started:
            try:
                run([pgctl, '-D', cluster, '-m', 'fast', '-w', 'stop'])
                result['database_stopped'] = True
            except Exception:
                result.update(database_stopped=False, passed=False)
        if log:
            log.close()
        (root / 'native-install-result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
