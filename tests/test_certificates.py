"""The private CA and server certificate behind HTTPS-only FamilyFoto."""
from datetime import datetime, timedelta, timezone
import ssl
import pytest
from cryptography import x509
from app import certificates
from app.service import server


def test_creates_a_trusted_chain_covering_the_names_asked_for(tmp_path):
    paths = certificates.ensure(tmp_path, ['family.example', '192.168.77.5'])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(paths['cert'], paths['key'])  # key matches certificate
    ca = x509.load_pem_x509_certificate(paths['ca'].read_bytes())
    leaf = x509.load_pem_x509_certificate(paths['cert'].read_bytes())
    leaf.verify_directly_issued_by(ca)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert {'localhost', 'family.example'} <= set(san.get_values_for_type(x509.DNSName))
    assert {'127.0.0.1', '192.168.77.5'} <= {str(a) for a in san.get_values_for_type(x509.IPAddress)}
    assert (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days <= 825  # Apple's limit
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True


def test_existing_certificates_are_kept_until_names_or_expiry_change(tmp_path):
    first = certificates.ensure(tmp_path)
    serial = x509.load_pem_x509_certificate(first['cert'].read_bytes()).serial_number
    ca_bytes = first['ca'].read_bytes()
    assert x509.load_pem_x509_certificate(certificates.ensure(tmp_path)['cert'].read_bytes()).serial_number == serial
    # A new name re-issues the server certificate from the same CA.
    certificates.ensure(tmp_path, ['new.example'])
    renewed = x509.load_pem_x509_certificate((tmp_path / 'server.pem').read_bytes())
    assert renewed.serial_number != serial and (tmp_path / 'ca.pem').read_bytes() == ca_bytes
    # So does approaching expiry.
    names, addresses = certificates.local_names(['new.example'])
    assert certificates.server_certificate_ok(tmp_path, names, addresses)
    later = datetime.now(timezone.utc) + timedelta(days=certificates.SERVER_DAYS - 10)
    assert not certificates.server_certificate_ok(tmp_path, names, addresses, now=later)


def test_a_certificate_from_another_authority_is_replaced(tmp_path, tmp_path_factory):
    other = certificates.ensure(tmp_path_factory.mktemp('other'))
    certificates.ensure(tmp_path)
    (tmp_path / 'server.pem').write_bytes(other['cert'].read_bytes())
    assert not certificates.server_certificate_ok(tmp_path, *certificates.local_names())
    certificates.ensure(tmp_path)
    assert certificates.server_certificate_ok(tmp_path, *certificates.local_names())


def test_a_missing_or_mismatched_server_key_is_reissued_from_the_same_ca(tmp_path, tmp_path_factory):
    paths = certificates.ensure(tmp_path)
    ca = paths['ca'].read_bytes()
    names = certificates.local_names()
    paths['key'].unlink()
    assert not certificates.server_certificate_ok(tmp_path, *names)
    certificates.ensure(tmp_path)
    assert certificates.server_certificate_ok(tmp_path, *names) and paths['key'].is_file()
    # A key from somewhere else no longer matches the certificate.
    other = certificates.ensure(tmp_path_factory.mktemp('other'))
    paths['key'].write_bytes(other['key'].read_bytes())
    assert not certificates.server_certificate_ok(tmp_path, *names)
    certificates.ensure(tmp_path)
    assert certificates.server_certificate_ok(tmp_path, *names)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(paths['cert'], paths['key'])
    assert paths['ca'].read_bytes() == ca  # trust is unchanged


@pytest.mark.parametrize('lost', ['ca.pem', 'ca-key.pem'])
def test_a_partly_missing_authority_is_reported_not_replaced(tmp_path, lost):
    certificates.ensure(tmp_path)
    before = {name: (tmp_path / name).read_bytes() for name in certificates.FILES if name != lost}
    (tmp_path / lost).unlink()
    with pytest.raises(certificates.CertificateRecoveryError, match=lost):
        certificates.ensure(tmp_path)
    assert not (tmp_path / lost).exists()
    assert {name: (tmp_path / name).read_bytes() for name in before} == before


def test_an_authority_whose_files_do_not_belong_together_is_reported(tmp_path, tmp_path_factory):
    certificates.ensure(tmp_path)
    other = certificates.ensure(tmp_path_factory.mktemp('other'))
    (tmp_path / 'ca-key.pem').write_bytes(other['ca'].parent.joinpath('ca-key.pem').read_bytes())
    with pytest.raises(certificates.CertificateRecoveryError, match='do not belong together'):
        certificates.ensure(tmp_path)


def test_private_keys_are_not_readable_by_others(tmp_path):
    import os
    import stat
    if os.name == 'nt':
        pytest.skip('Windows ignores POSIX modes')
    certificates.ensure(tmp_path)
    for name in ('ca-key.pem', 'server-key.pem'):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_server_renews_its_managed_certificate_when_started(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    managed = tmp_path / 'config' / 'tls'
    certificates.ensure(managed)
    old = x509.load_pem_x509_certificate((managed / 'server.pem').read_bytes()).serial_number
    config = {'tls': {'certfile': 'config/tls/server.pem', 'keyfile': 'config/tls/server-key.pem', 'names': ['lan.example']}}
    options = server.server_options(config)
    assert options['ssl_certfile'] == str((managed / 'server.pem').resolve())
    assert x509.load_pem_x509_certificate((managed / 'server.pem').read_bytes()).serial_number != old


def test_server_speaks_https_only_and_clients_verify_with_the_ca(tmp_path, monkeypatch):
    import socket
    import threading
    import time
    import httpx
    import uvicorn
    from fastapi import FastAPI
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    certificates.ensure(tmp_path / 'config' / 'tls')
    app = FastAPI()

    @app.get('/login')
    def login():
        return {'ok': True}

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    options = server.server_options({'port': port, 'tls': {'certfile': 'config/tls/server.pem', 'keyfile': 'config/tls/server-key.pem'}})
    running = uvicorn.Server(uvicorn.Config(app, **{**options, 'log_level': 'error'}))
    thread = threading.Thread(target=lambda: running.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if running.started:
                break
            time.sleep(.05)
        trusted = ssl.create_default_context(cafile=str(tmp_path / 'config' / 'tls' / 'ca.pem'))
        assert httpx.get(f'https://127.0.0.1:{port}/login', verify=trusted).json() == {'ok': True}
        with pytest.raises(httpx.ConnectError):  # an unknown authority is rejected
            httpx.get(f'https://127.0.0.1:{port}/login')
        with pytest.raises(httpx.HTTPError):  # plain HTTP gets nothing usable
            httpx.get(f'http://127.0.0.1:{port}/login').raise_for_status()
    finally:
        running.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_a_hostname_longer_than_the_certificate_name_limit_still_works(tmp_path, monkeypatch):
    long_name = 'a' * 40 + '.' + 'b' * 40 + '.local'
    monkeypatch.setattr(certificates.socket, 'gethostname', lambda: long_name)
    paths = certificates.ensure(tmp_path)
    leaf = x509.load_pem_x509_certificate(paths['cert'].read_bytes())
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert long_name in san.get_values_for_type(x509.DNSName)
    assert certificates.server_certificate_ok(tmp_path, *certificates.local_names())
