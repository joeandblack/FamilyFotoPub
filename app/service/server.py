"""Direct HTTPS launcher: it never serves plain HTTP; forwarded headers are never trusted."""
from pathlib import Path
import os
import ssl
import uvicorn
from ..config import ROOT, load_config


DEV_HTTP = 'FAMILYFOTO_DEV_HTTP'
LOOPBACK = ('127.0.0.1', 'localhost', '::1')


def server_options(config, *, renew=True):
    options = dict(host=config.get('host', '127.0.0.1'),
                   port=config.get('port', 4340), log_level='info',
                   proxy_headers=False)
    tls = config.get('tls')
    if tls is None and os.environ.get(DEV_HTTP) == '1':
        # Development only: an environment variable (never a config file, so it cannot ship in an
        # installation), reachable from this computer alone. Installer verification stays strict.
        if options['host'] not in LOOPBACK:
            raise ValueError(f'{DEV_HTTP}=1 allows plain HTTP on this computer only; host must be 127.0.0.1.')
        print(f'WARNING: {DEV_HTTP}=1 - serving plain HTTP on {options["host"]}. Development only.', flush=True)
        return options
    if tls is None:
        raise ValueError('HTTPS is required: set tls.certfile and tls.keyfile in config/local.json '
                         '(python -m app.certificates creates a local certificate authority and server certificate).')
    if not isinstance(tls, dict):
        raise ValueError('tls must contain certificate and key file paths.')
    # An incomplete TLS configuration must fail closed, never fall back to HTTP.
    # A backup's progress page must not rewrite configuration while it is being captured.
    if renew:
        renew_managed_certificate(config)
    for setting, option in (('certfile', 'ssl_certfile'), ('keyfile', 'ssl_keyfile')):
        value = tls.get(setting)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'tls.{setting} is required.')
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise ValueError(f'tls.{setting} must reference an existing file.')
        options[option] = str(path.resolve())
    options['ssl_version'] = ssl.PROTOCOL_TLS_SERVER
    return options


def renew_managed_certificate(config):
    """Re-issue the installer-managed server certificate before it expires or its names change."""
    tls = config.get('tls') or {}
    managed = Path(os.environ.get('FAMILYFOTO_CONFIG_DIR', ROOT / 'config')) / 'tls'
    if (isinstance(tls.get('certfile'), str) and (ROOT / tls['certfile']).resolve() == (managed / 'server.pem').resolve()
            and (managed / 'ca-key.pem').is_file()):
        from ..certificates import ensure
        ensure(managed, tls.get('names', []))


def main():
    uvicorn.run('app.api:app', **server_options(load_config()))

if __name__=='__main__':
    main()
