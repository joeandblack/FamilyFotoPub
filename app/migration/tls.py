"""Prepare a new host certificate without replacing restored client trust.

Writes only a new private staging directory. No listener, settings change, trust
installation or application activation is performed here.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .. import certificates
from ..backup.encrypted import read_bounded
from ..backup.media_copy import sync_directory
from ..backup.private_storage import create_private_directory
from ..backup.recovery import save_private
from ..backup.space import directory_identity


def target_names(values):
    """Explicit SANs only: no network discovery or inherited source host names."""
    names, addresses = set(), set()
    if not values or len(values) > 100:
        raise ValueError('Provide between one and 100 explicit target names or IP addresses')
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 253:
            raise ValueError('Invalid target host name')
        try:
            address = ipaddress.ip_address(value)
            if '%' in value:
                raise ValueError('Scoped IP addresses are not certificate identities')
            addresses.add(str(address))
            continue
        except ValueError:
            pass
        name = value.lower()
        if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
               for part in name.split('.')):
            raise ValueError('Use an ASCII DNS host name or IP address, without URLs or wildcards')
        names.add(name)
    return sorted(names), sorted(addresses)


def restored_ca(directory, *, now=None):
    directory = Path(directory).absolute()
    directory_identity(directory)
    cert_bytes = read_bounded(directory / 'ca.pem', 65536)
    key_bytes = read_bounded(directory / 'ca-key.pem', 65536)
    try:
        cert = x509.load_pem_x509_certificate(cert_bytes)
        key = serialization.load_pem_private_key(key_bytes, password=None)
        if not isinstance(key, (ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey)):
            raise ValueError('Unsupported CA key type')
        if certificates._public_der(cert) != certificates._public_der(key):
            raise ValueError('CA certificate and private key differ')
        cert.verify_directly_issued_by(cert)
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if not constraints.ca or not usage.key_cert_sign:
            raise ValueError('Certificate is not authorized to issue server certificates')
        now = now or datetime.now(timezone.utc)
        if cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now + timedelta(days=31):
            raise ValueError('CA is not currently usable with sufficient remaining validity')
    except Exception as error:
        raise certificates.CertificateRecoveryError(
            'Restored certificate authority is invalid, mismatched, expired or near expiry; '
            'restore a valid pair or use an explicit new-CA/client re-trust procedure') from error
    return cert, key, cert_bytes, key_bytes


def prepare_recovery_tls(source, target, names):
    """Preserve exact CA bytes; issue a new leaf/key in a new private directory."""
    source, target = (Path(os.path.abspath(path)) for path in (source, target))
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError('TLS staging must be separate from restored TLS state')
    dns, ips = target_names(names)
    ca, key, certificate, secret = restored_ca(source)
    create_private_directory(target)
    for name, raw in (('ca.pem', certificate), ('ca-key.pem', secret)):
        with (target / name).open('xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        (target / name).chmod(0o600)
    leaf = certificates._issue_server(target, ca, key, dns, ips)
    for name in ('server.pem', 'server-key.pem'):
        with (target / name).open('r+b') as handle:
            os.fsync(handle.fileno())
    sync_directory(target)
    if not certificates.server_certificate_ok(target, dns, ips):
        raise certificates.CertificateRecoveryError('New target certificate failed verification')
    if (read_bounded(source / 'ca.pem', 65536) != certificate
            or read_bounded(source / 'ca-key.pem', 65536) != secret):
        raise certificates.CertificateRecoveryError('Source CA changed during preparation')
    report = {'format': 'familyfoto-recovery-tls-v1', 'state': 'prepared',
              'ca_sha256': hashlib.sha256(certificate).hexdigest(),
              'certificate_fingerprint': leaf.fingerprint(hashes.SHA256()).hex(),
              'dns_names': dns, 'ip_addresses': ips, 'trust_preserved': True,
              'activation_allowed': False, 'services_started': False}
    save_private(target / 'TLS-REPORT.json', report)
    return report


def verify_recovery_tls(directory, *, expected_ca_sha256):
    """Recheck actual staged key/cert bytes, not just a prior completion report."""
    directory = Path(directory).absolute()
    ca, _, raw, _ = restored_ca(directory)
    report = json.loads(read_bounded(directory / 'TLS-REPORT.json', 65536))
    if (report.get('format') != 'familyfoto-recovery-tls-v1' or report.get('state') != 'prepared'
            or report.get('activation_allowed') is not False or report.get('services_started') is not False
            or report.get('trust_preserved') is not True
            or report.get('ca_sha256') != expected_ca_sha256
            or hashlib.sha256(raw).hexdigest() != expected_ca_sha256):
        raise certificates.CertificateRecoveryError('Staged certificate authority differs from recovery evidence')
    dns, ips = target_names(report.get('dns_names', []) + report.get('ip_addresses', []))
    leaf = x509.load_pem_x509_certificate(read_bounded(directory / 'server.pem', 65536))
    leaf.verify_directly_issued_by(ca)
    now = datetime.now(timezone.utc)
    if (leaf.fingerprint(hashes.SHA256()).hex() != report.get('certificate_fingerprint')
            or leaf.not_valid_before_utc > now or leaf.not_valid_after_utc > ca.not_valid_after_utc
            or not certificates.server_certificate_ok(directory, dns, ips)):
        raise certificates.CertificateRecoveryError('Staged host certificate or private key changed')
    return report
