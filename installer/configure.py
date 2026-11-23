"""Create installation credentials. Does not write to either media disk."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    parser=argparse.ArgumentParser(description='Create application credentials; media storage can be selected at first import.')
    parser.add_argument('--source')
    parser.add_argument('--destination')
    parser.add_argument('--database-port', type=port_number, default=5432)
    parser.add_argument('--web-port', type=port_number, default=4340)
    parser.add_argument('--instance', type=instance_name,
                        help='Name of a second installation on this computer; it gets its own service, task and shortcut.')
    parser.add_argument('--tls-name', action='append', default=[],
                        help='Extra DNS name or IP address the HTTPS certificate must cover (repeatable).')
    parser.add_argument('--tls-cert', help='Use your own certificate (PEM) instead of a generated one; requires --tls-key.')
    parser.add_argument('--tls-key')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    parser.add_argument('--password-stdin', action='store_true',
                        help='Read one UTF-8 password line from redirected stdin for unattended setup.')
    args=parser.parse_args()
    if args.database_port == args.web_port:
        parser.error('Database and web ports must differ.')
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error('Supply both --tls-cert and --tls-key, or neither.')
    for tls_file in (args.tls_cert, args.tls_key):
        if tls_file and (not Path(tls_file).is_absolute() or not Path(tls_file).is_file()):
            parser.error('Certificate and key paths must be absolute existing files.')
    if bool(args.ffmpeg) != bool(args.ffprobe):
        parser.error('Supply both --ffmpeg and --ffprobe, or neither.')
    for tool in (args.ffmpeg, args.ffprobe):
        if tool and (not Path(tool).is_absolute() or not Path(tool).is_file()):
            parser.error('Video tool paths must be absolute existing files.')
    path = ROOT / 'config' / 'local.json'
    if path.exists():
        raise SystemExit('Configuration exists; use Settings configuration in the running app. A populated destination requires a verified migration.')
    from app.backup.admission import hold_process_lease
    hold_process_lease(ROOT / 'data')
    for name in ('config', 'data', 'logs', 'temp', 'runtime'):
        (ROOT / name).mkdir(exist_ok=True)
    storage = {'source': {}, 'destination': {}}
    if args.source or args.destination:
        if not args.source or not args.destination:
            raise SystemExit('Legacy explicit enrollment requires both source and destination; omit both for first-import setup.')
        storage = enroll_storage(args.source, args.destination)
    from app.auth import password_hash
    if args.password_stdin:
        if sys.stdin.isatty():
            raise SystemExit('--password-stdin requires redirected input.')
        password = sys.stdin.buffer.readline().decode('utf-8-sig').rstrip('\r\n')
        confirmation = password
    else:
        password = getpass.getpass('Administrator password (at least 12 characters): ')
        confirmation = getpass.getpass('Repeat password: ')
    if len(password)<12 or password != confirmation:
        raise SystemExit('Passwords must match and contain at least 12 characters.')
    config = {
        'database': {'host': '127.0.0.1', 'port': args.database_port, 'dbname': 'familyfoto',
                     'user': 'familyfoto', 'password': secrets.token_urlsafe(32)},
        'postgres_admin_password': secrets.token_urlsafe(32),
        **storage,
        'io_mibps': 30, 'port': args.web_port, 'host': '127.0.0.1',
        'browser_user': 'family', 'browser_password_hash': password_hash(password),
        'auth_secret': secrets.token_urlsafe(32),
    }
    if args.instance:
        config['instance'] = args.instance
    if args.ffmpeg:
        config['video_tools'] = {'ffmpeg': args.ffmpeg, 'ffprobe': args.ffprobe}
    # FamilyFoto serves HTTPS only. Without a supplied certificate, create a private CA
    # and server certificate; each client trusts ca.pem once.
    if args.tls_cert:
        config['tls'] = {'certfile': args.tls_cert, 'keyfile': args.tls_key}
        authority = None
    else:
        from app.certificates import ensure
        paths = ensure(ROOT / 'config' / 'tls', args.tls_name)
        config['tls'] = {'certfile': 'config/tls/server.pem', 'keyfile': 'config/tls/server-key.pem'}
        if args.tls_name:
            config['tls']['names'] = args.tls_name
        authority = paths['ca']
    # Exclusive creation prevents an existing installation being overwritten.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(config, stream, indent=2)
    print('Created ignored config/local.json. No disk contents modified.')
    if authority:
        print(f'HTTPS certificate created. Install this authority on each browser computer once: {authority}')


def port_number(value):
    number = int(value)
    if not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError('Use an unprivileged port between 1024 and 65535.')
    return number


def instance_name(value):
    from app.service.instance import NAME
    if not NAME.fullmatch(value):
        raise argparse.ArgumentTypeError('Use 1 to 24 lowercase letters, digits or hyphens, starting with a letter or digit.')
    return value


def enroll_storage(source_path, destination_path):
    """Compatibility for explicitly requested maintainer enrollment, not setup default."""
    from app.storage.disks import snapshot
    from app.storage.config import enroll_source
    candidate=Path(destination_path)
    if not candidate.is_absolute() or '..' in candidate.parts or str(candidate).startswith('\\\\'):
        raise SystemExit('Destination must be an absolute folder on a local disk.')
    if any(p.is_symlink() or p.is_junction() for p in (candidate,*candidate.parents)):
        raise SystemExit('Linked destination folders are not allowed.')
    destination=candidate.resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise SystemExit('Choose a new or empty destination folder.')
    if (destination.parent/'.familyfoto-volume.json').exists():
        raise SystemExit('Destination has a library marker; migration review required.')
    source=enroll_source({'destination':{'root':str(destination)}},source_path)
    drive=destination.drive.rstrip(':')
    disk=snapshot([drive])[drive]
    if not disk['online']:
        raise SystemExit('An enrolled disk is offline.')
    return {'source':source,'destination':{'root':str(destination),'drive':drive,
            **{k:disk[k] for k in ('serial','volume_serial','volume_id')}}}

if __name__ == '__main__':
    main()
