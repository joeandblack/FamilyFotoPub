"""Read-only fresh-install preflight. Never reads credentials or starts services."""
import argparse
import errno
import os
from pathlib import Path
import platform
import socket
import sys

ROOT = Path(__file__).resolve().parents[2]
BREW_ROOT = Path('/opt/homebrew')


def port_status(port):
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
        return 'available (not reserved)'
    except OSError as error:
        return 'occupied' if error.errno == errno.EADDRINUSE else 'unverified (socket check denied or unavailable)'


def inspect(root, ports, *, brew_root=BREW_ROOT):
    blockers = []
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        blockers.append('Native Apple Silicon macOS required')
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        blockers.append('Run as the signed-in user, not sudo')
    if os.environ.get('FAMILYFOTO_DATA_DIR'):
        blockers.append('External data-directory override is set; fresh setup refuses it')
    if any(p.is_symlink() for p in (root, *root.parents)):
        blockers.append('Linked installation root is not supported')
    state = [name for name in ('config/local.json', '.venv', 'data', 'logs', 'temp', 'runtime')
             if (root / name).exists() or (root / name).is_symlink()]
    if state:
        blockers.append('Existing installation state: ' + ', '.join(state) + '; use a fresh code-only copy')
    if (root / 'config').is_symlink():
        blockers.append('Linked configuration directory is not supported')
    for name in ('requirements.txt','installer/configure.py','app/api.py'):
        if not (root / name).is_file():
            blockers.append('Incomplete application package: ' + name)
    if os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT'):
        # The shared installer bundles these; Homebrew is neither needed nor consulted.
        base, tools = root / 'bundled', {'Python 3.12 (bundled)': 'python/bin/python3.12',
                                         'PostgreSQL 17 (bundled)': 'postgres/bin/postgres',
                                         'FFmpeg (bundled)': 'ffmpeg/bin/ffmpeg'}
        required = tuple(tools)
    else:
        base, tools = brew_root, {'Homebrew': 'bin/brew', 'Python 3.12': 'opt/python@3.12/bin/python3.12',
                                  'PostgreSQL 17': 'opt/postgresql@17/bin/postgres', 'FFmpeg': 'opt/ffmpeg/bin/ffmpeg'}
        required = ('Homebrew', 'Python 3.12')
    available = {name: os.access(base / path, os.X_OK) and (base / path).is_file()
                 for name, path in tools.items()}
    for name in required:
        if not available[name]:
            blockers.append(name + ' must be installed explicitly before setup')
    if len(set(ports.values())) != len(ports):
        blockers.append('Requested ports overlap')
    statuses = {name: port_status(port) for name, port in ports.items()}
    for name, status in statuses.items():
        if not status.startswith('available'):
            blockers.append(name + ' port ' + str(ports[name]) + ': ' + status)
    return {'blockers': blockers, 'tools': available, 'ports': statuses}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-port', type=int, default=5433)
    parser.add_argument('--web-port', type=int, default=4341)
    parser.add_argument('--people-port', type=int, default=54341)
    args = parser.parse_args()
    if any(not 1024 <= port <= 65535 for port in vars(args).values()) or not 49152 <= args.people_port <= 65535:
        parser.error('Core ports must be 1024-65535 and People port 49152-65535')
    ports = {'database':args.database_port, 'web':args.web_port, 'optional People':args.people_port}
    result = inspect(ROOT, ports)
    print('Host: ' + platform.system() + ' ' + platform.release() + ', ' + platform.machine())
    for name, found in result['tools'].items():
        print(name + ': ' + ('found' if found else 'missing'))
    for name, status in result['ports'].items():
        print(name + ' port ' + str(ports[name]) + ': ' + status)
    for blocker in result['blockers']:
        print('PREPARATION ISSUE: ' + blocker)
    if not os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT'):
        print('Missing PostgreSQL/FFmpeg formulae may be installed by setup using Homebrew.')
    print('Checks do not reserve ports or qualify dependencies. No files or services changed.')
    return 0  # Plan is informational; install independently enforces its prerequisites.


if __name__ == '__main__':
    raise SystemExit(main())
