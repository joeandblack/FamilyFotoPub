"""Private certificate authority and server certificate for HTTPS-only FamilyFoto.

The CA key stays in config/tls on the server; each client trusts ca.pem once. The
server certificate is re-issued from the CA when it is near expiry or when the
computer's names change, so an unattended server does not stop accepting browsers.
"""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import socket
import tempfile
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DAYS = 3650
SERVER_DAYS = 800  # Apple clients reject server certificates valid for more than 825 days.
RENEW_BEFORE = timedelta(days=30)
FILES = ('ca.pem', 'ca-key.pem', 'server.pem', 'server-key.pem')


class CertificateRecoveryError(RuntimeError):
    """The CA files are incomplete or inconsistent; replacing them would change the identity clients trust."""


def local_names(extra=()):
    """DNS names and IP addresses the certificate must cover: this computer and any extras.

    No name lookups: resolving the computer's own name can wait for DNS/mDNS timeouts (tens of
    seconds on some networks and CI runners), and this runs at every server start.
    """
    names, addresses = {'localhost'}, {'127.0.0.1', '::1'}
    host = socket.gethostname().lower()
    if host and host != 'localhost':
        names.add(host)
        if '.' not in host:
            names.add(host + '.local')  # how Apple devices find a Mac on the local network
    try:
        # A UDP connect sends nothing; it only asks the OS which address would be used.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('192.0.2.1', 9))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    for value in extra:
        try:
            addresses.add(str(ipaddress.ip_address(value)))
        except ValueError:
            names.add(value.lower())
    return sorted(names), sorted(addresses)


def _write(path, data, private=False):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tls-')
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
    if private:
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _pem_cert(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _pem_key(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _new_ca(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FamilyFoto local CA'),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'FamilyFoto')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write(directory / 'ca-key.pem', _pem_key(key), private=True)
    _write(directory / 'ca.pem', _pem_cert(cert))
    return cert, key


def _common_name(names):
    """The first name, unless it exceeds the 64-character X.509 limit (the SANs carry the real names)."""
    return names[0] if names and len(names[0]) <= 64 else 'FamilyFoto'


def _issue_server(directory, ca_cert, ca_key, names, addresses):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _common_name(names))]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(max(now - timedelta(days=1), ca_cert.not_valid_before_utc))
            .not_valid_after(min(now + timedelta(days=SERVER_DAYS), ca_cert.not_valid_after_utc))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=True, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write(directory / 'server-key.pem', _pem_key(key), private=True)
    _write(directory / 'server.pem', _pem_cert(cert))
    return cert


def _covers(cert, names, addresses):
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    have_names = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
    have_ips = {str(a) for a in san.get_values_for_type(x509.IPAddress)}
    return set(names) <= have_names and set(addresses) <= have_ips


def _public_der(key_or_cert):
    return key_or_cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def server_certificate_ok(directory, names, addresses, now=None):
    """True when server.pem is signed by this CA, its key is present and matches, names are covered and it is not near expiry."""
    directory, now = Path(directory), now or datetime.now(timezone.utc)
    try:
        ca = x509.load_pem_x509_certificate((directory / 'ca.pem').read_bytes())
        cert = x509.load_pem_x509_certificate((directory / 'server.pem').read_bytes())
        key = serialization.load_pem_private_key((directory / 'server-key.pem').read_bytes(), None)
        cert.verify_directly_issued_by(ca)
        if _public_der(key) != _public_der(cert):
            return False
        return cert.not_valid_after_utc - now > RENEW_BEFORE and _covers(cert, names, addresses)
    except Exception:  # missing, unreadable, unsigned by this CA: all mean "issue a new one"
        return False


def _load_or_create_ca(directory):
    """A brand-new directory gets a CA; a half-present or inconsistent one is reported, never replaced."""
    cert_path, key_path = directory / 'ca.pem', directory / 'ca-key.pem'
    if not cert_path.exists() and not key_path.exists():
        return _new_ca(directory)
    missing = [p.name for p in (cert_path, key_path) if not p.is_file()]
    if missing:
        raise CertificateRecoveryError(
            f'{directory}: {" and ".join(missing)} missing while the rest of the certificate authority is present. '
            'Restore it from a backup; replacing it would require every device to trust a new CA. '
            'To start over deliberately, delete the whole config/tls folder.')
    try:
        ca_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        ca_key = serialization.load_pem_private_key(key_path.read_bytes(), None)
    except (ValueError, TypeError) as error:
        raise CertificateRecoveryError(f'{directory}: the certificate authority files are unreadable ({error}). '
                                       'Restore them from a backup, or delete the whole config/tls folder to start over.') from error
    if _public_der(ca_cert) != _public_der(ca_key):
        raise CertificateRecoveryError(f'{directory}: ca.pem and ca-key.pem do not belong together. '
                                       'Restore them from a backup, or delete the whole config/tls folder to start over.')
    return ca_cert, ca_key


def ensure(directory, extra_names=()):
    """Create the CA and server certificate if missing, or renew the server certificate."""
    directory = Path(directory)
    from .backup.admission import guard_application_path
    guard_application_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    names, addresses = local_names(extra_names)
    ca_cert, ca_key = _load_or_create_ca(directory)
    if not server_certificate_ok(directory, names, addresses):
        _issue_server(directory, ca_cert, ca_key, names, addresses)
    return {'ca': directory / 'ca.pem', 'cert': directory / 'server.pem', 'key': directory / 'server-key.pem'}


def main():
    import argparse
    from .config import ROOT
    parser = argparse.ArgumentParser(description='Create or renew the local HTTPS certificate.')
    parser.add_argument('--name', action='append', default=[], help='Extra DNS name or IP address to cover.')
    args = parser.parse_args()
    paths = ensure(ROOT / 'config' / 'tls', args.name)
    print(f'Server certificate ready. Install this CA on each client once: {paths["ca"]}')


if __name__ == '__main__':
    main()
