"""Read-only installer checks. Never starts services or applies SQL upgrades."""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def database_checks(config, root, *, before_schema=False, require_empty=False):
    import psycopg

    with psycopg.connect(**config['database'], connect_timeout=5) as db:
        db.execute('SET TRANSACTION READ ONLY')
        row = db.execute("SELECT current_user, current_database(), current_setting('server_version_num')::int, current_setting('listen_addresses')").fetchone()
        if row[:2] != (config['database']['user'], config['database']['dbname']):
            raise ValueError('Unexpected database identity')
        if row[2] // 10000 != 17 or row[3] != '127.0.0.1':
            raise ValueError('Expected PostgreSQL 17 listening only on 127.0.0.1')
        role = db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
        if any(role):
            raise ValueError('Application database account has elevated privileges')
        if not before_schema:
            expected = {int(path.name.split('_', 1)[0]) for path in (root / 'databases').glob('*.sql')}
            actual = {row[0] for row in db.execute('SELECT version FROM schema_versions').fetchall()}
            if not expected or expected != actual:
                raise ValueError('Database schema does not match the supplied application')
            if require_empty and db.execute('SELECT EXISTS(SELECT 1 FROM media) OR EXISTS(SELECT 1 FROM jobs)').fetchone()[0]:
                raise ValueError('Library already contains data; fresh installation cannot resume')


def tls_check(config, root):
    import ssl
    from datetime import datetime, timezone
    from cryptography import x509
    tls = config.get('tls')
    if not isinstance(tls, dict):
        raise ValueError('HTTPS is required: config/local.json has no tls certificate (run python -m app.certificates)')
    cert, key = (root / tls['certfile']).resolve(), (root / tls['keyfile']).resolve()
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    left = (x509.load_pem_x509_certificate(cert.read_bytes()).not_valid_after_utc - datetime.now(timezone.utc)).days
    if left < 0:
        raise ValueError('HTTPS certificate has expired')
    print(f'PASS: HTTPS certificate loads ({left} days left)')


def verify(root, *, database_only=False, before_schema=False, require_empty=False):
    config = json.loads((root / 'config/local.json').read_text(encoding='utf-8'))
    if config['database']['host'] not in ('127.0.0.1', 'localhost'):
        raise ValueError('Core installer expects a local database')
    if not config.get('browser_password_hash') or config.get('browser_password'):
        raise ValueError('Administrator password hash required; plaintext password is not supported')
    database_checks(config, root, before_schema=before_schema, require_empty=require_empty)
    print('PASS: database identity, restricted role, loopback binding' + ('' if before_schema else ' and schema versions'))
    if database_only:
        return
    tls_check(config, root)
    if sys.version_info[:2] != (3, 12):
        raise ValueError('This installer targets Python 3.12')
    for line in (root / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        package, required = line.strip().split('==', 1)
        package = package.split('[', 1)[0]
        installed = version(package)
        if installed != required:
            raise ValueError(f'Dependency version mismatch: {package}')
        print(f'PASS: {package} {installed}')
    for tool in ('ffmpeg', 'ffprobe'):
        suffix = '.exe' if os.name == 'nt' else ''
        executable = Path(config.get('video_tools', {}).get(
            tool, config.get(tool, root / 'runtime/ffmpeg/bin' / f'{tool}{suffix}')))
        result = subprocess.run([str(executable), '-version'], capture_output=True, text=True, timeout=15, check=True)
        if not result.stdout.startswith(tool + ' version '):
            raise ValueError(f'{tool} did not report its version')
        print(f'PASS: {tool} executable')
    print('Core checks passed. No media, services, configuration or database data changed.')
    print('Desktop/browser/media smoke tests are still required. People and LAN TLS are not validated here.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-only', action='store_true')
    parser.add_argument('--before-schema', action='store_true')
    parser.add_argument('--require-empty', action='store_true')
    args = parser.parse_args()
    try:
        verify(ROOT, **vars(args))
    except Exception as error:
        # Connection exceptions/config data can contain secrets. Do not echo them.
        print(f'FAILED: installation verification ({type(error).__name__}). Check configuration, dependencies and local service status; credentials were not printed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
