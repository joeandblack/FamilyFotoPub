"""Private child entrypoint for a validated recovery preview; configuration arrives on stdin."""
import asyncio
import json
from pathlib import Path
import sys
import time


def check_configuration(cfg):
    if (cfg.get('host') != '127.0.0.1' or type(cfg.get('port')) is not int or not 1024 <= cfg['port'] <= 65535
            or cfg.get('read_only_preview') is not True or cfg.get('limited_metadata_edit')
            or cfg.get('recovery', {}).get('activation_required') is not True
            or cfg.get('database', {}).get('options') != '-c default_transaction_read_only=on'):
        raise ValueError('Recovery preview configuration is not local and read-only')
    writable = Path(cfg['preview']['writable_root']).resolve(strict=True)
    for name in ('data_root',):
        protected = Path(cfg['preview'][name]).resolve(strict=True)
        if writable.is_relative_to(protected) or protected.is_relative_to(writable):
            raise ValueError('Preview scratch must be separate from recovered data')
    protected = Path(cfg['destination']['root']).resolve(strict=True)
    if writable.is_relative_to(protected) or protected.is_relative_to(writable):
        raise ValueError('Preview scratch must be separate from recovered media')
    return writable


async def serve(cfg, *, duration=3300):
    sys.dont_write_bytecode = True
    writable = check_configuration(cfg)
    from app.service.readonly_preview import build_app, disk_check_commands, install_write_guard
    install_write_guard(writable, allowed_subprocesses=disk_check_commands(cfg))
    app = build_app(cfg, recovery_scratch=writable)
    # Do not call the production launcher: it can renew managed certificates.
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=cfg['port'],
        ssl_certfile=cfg['tls']['certfile'], ssl_keyfile=cfg['tls']['keyfile'],
        proxy_headers=False, access_log=False, timeout_graceful_shutdown=10))
    async def stop():
        deadline = time.monotonic() + duration
        while not server.should_exit:
            if (writable / 'STOP').exists() or time.monotonic() >= deadline:
                server.should_exit = True
                return
            await asyncio.sleep(.25)
    watcher = asyncio.create_task(stop())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def main():
    raw = sys.stdin.buffer.read(2 * 1024 ** 2 + 1)
    if len(raw) > 2 * 1024 ** 2:
        raise ValueError('Preview configuration exceeds its limit')
    asyncio.run(serve(json.loads(raw)))


if __name__ == '__main__':
    main()
