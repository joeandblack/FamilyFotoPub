"""Offline artifact smoke check. Run ONLY on a fresh extracted disposable release."""
import importlib
import asyncio
import json
import os
from pathlib import Path
import sys
import tomllib


def main():
    root = Path(__file__).resolve().parents[1]
    if (root / '.git').exists() or (root / 'config/local.json').exists():
        raise RuntimeError('Smoke check requires an unconfigured extracted release, not an installation')
    if any((root / name).exists() for name in ('data', 'logs', 'runtime')):
        raise RuntimeError('Smoke check refuses existing application state')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    for name in list(os.environ):
        if name.startswith('FAMILYFOTO_'):
            del os.environ[name]
    os.environ['FAMILYFOTO_DATA_DIR'] = str(root / 'data')
    # Windows creates a loopback socketpair for its event loop's wakeup pipe.
    # Create only that pipe before denying all subsequent network operations.
    loop = asyncio.new_event_loop()

    # Windows private folders (app/backup/private_storage.py) need the account SID and an ACL,
    # from these two system programs. They are local and touch only folders inside the release.
    system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    allowed = {str(system / 'whoami.exe').lower(), str(system / 'icacls.exe').lower()} if os.name == 'nt' else set()

    def audit(event, args):
        if event == 'subprocess.Popen':
            # On Windows the audit event carries the joined command line, quoted by list2cmdline.
            line = os.fsdecode(args[1]) if isinstance(args[1], (str, bytes)) else ''
            program = line[1:line.find('"', 1)] if line.startswith('"') else line.split(' ', 1)[0]
            if args[0] is None and program.lower() in allowed:
                return
        if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.sendto',
                     'socket.sendmsg', 'subprocess.Popen', 'os.system'):
            raise RuntimeError(f'Offline smoke check prohibits {event}')
        if event in ('os.remove', 'os.rmdir', 'os.rename'):
            count = 2 if event == 'os.rename' else 1
            for path in args[:count]:
                if not Path(os.fsdecode(path)).resolve().is_relative_to(root):
                    raise RuntimeError('Smoke check refuses mutation outside extracted release')
            if any(fd not in (None, -1) for fd in args[count:]):
                raise RuntimeError('Directory-relative mutation prohibited in smoke check')
        if event == 'open':
            path, mode, flags = args
            if isinstance(path, (str, bytes)) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                if not Path(os.fsdecode(path)).resolve().is_relative_to(root):
                    raise RuntimeError('Smoke check refuses writes outside extracted release')
    sys.addaudithook(audit)
    from installer.release import verify
    manifest = verify(root)
    config = json.loads((root / 'config/example.json').read_text(encoding='utf-8'))
    config['database'] = {'host': '127.0.0.1', 'port': 1, 'dbname': 'never_connect',
                          'user': 'never_connect', 'password': 'not-a-real-credential'}
    config['source'] = {}
    config['destination'] = {}
    (root / 'config/local.json').write_text(json.dumps(config), encoding='utf-8')

    def forbidden(*args, **kwargs):
        raise RuntimeError('Database/worker startup forbidden in offline artifact check')
    import psycopg
    import psycopg_pool
    psycopg.connect = forbidden
    psycopg_pool.ConnectionPool.open = forbidden
    from app import database, uploads
    from app.database import schema_upgrading
    database.connection = forbidden
    database.migrate = forbidden
    schema_upgrading.migrate = forbidden
    uploads.start_dispatcher = forbidden
    from face_detect.api import service
    service.start_with_app = forbidden
    document = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    entries = document['tool']['familyfoto']['entrypoints']
    for name in entries['modules']:
        importlib.import_module(name)  # Resolve imports; never execute their main functions.
    for value in entries['asgi']:
        module, attribute = value.split(':')
        assert callable(getattr(importlib.import_module(module), attribute))
    from app.api import create_app
    from app.release import metadata, schema_compatibility
    import httpx
    application = create_app()
    # ASGITransport does not run lifespan or open a network listener.
    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url='http://testserver') as client:
            for path in ('/login', '/web/app.js', '/web/familyfoto.ico'):
                response = await client.get(path)
                assert response.status_code == 200 and response.content, path
            assert (await client.get('/api/albums')).status_code == 401
    try:
        loop.run_until_complete(requests())
    finally:
        loop.close()
    assert application.version == manifest['application_version']
    target = metadata()['core_schema_version']
    assert schema_compatibility(range(1, target + 1)) == 'compatible'
    assert schema_compatibility(range(1, target + 2)) == 'newer-database-refused'
    for name, module in list(sys.modules.items()):
        if name.split('.')[0] in ('app', 'face_detect', 'installer') and getattr(module, '__file__', None):
            assert Path(module.__file__).resolve().is_relative_to(root), name
    print(json.dumps({'ok': True, 'version': application.version,
                      'modules_checked': len(entries['modules']), 'database_access': False,
                      'workers_started': False, 'root': str(root)}))


if __name__ == '__main__':
    main()
