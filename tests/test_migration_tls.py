from datetime import datetime, timedelta, timezone
import json

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from app import certificates
from app.migration.tls import prepare_recovery_tls, restored_ca, target_names, verify_recovery_tls


def authority(path, *, ca=True, signing=True, starts=-1, ends=120):
    path.mkdir()
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Fixture CA')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now + timedelta(days=starts))
            .not_valid_after(now + timedelta(days=ends))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=signing, crl_sign=signing,
                           content_commitment=False, key_encipherment=False, data_encipherment=False,
                           key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    (path / 'ca.pem').write_bytes(certificates._pem_cert(cert))
    (path / 'ca-key.pem').write_bytes(certificates._pem_key(key))
    return cert


def test_recovery_preserves_ca_and_issues_bounded_target_leaf(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    ca = authority(source)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    report = prepare_recovery_tls(source, target, ['new-mac.local', '127.0.0.1', '::1'])
    assert report['trust_preserved'] and not report['activation_allowed']
    assert json.loads((target / 'TLS-REPORT.json').read_text()) == report
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert (target / 'ca.pem').read_bytes() == before['ca.pem']
    assert (target / 'ca-key.pem').read_bytes() == before['ca-key.pem']
    leaf = x509.load_pem_x509_certificate((target / 'server.pem').read_bytes())
    leaf.verify_directly_issued_by(ca)
    assert leaf.not_valid_after_utc <= ca.not_valid_after_utc
    assert certificates.server_certificate_ok(target, ['new-mac.local'], ['127.0.0.1', '::1'])
    assert verify_recovery_tls(target, expected_ca_sha256=report['ca_sha256']) == report
    with pytest.raises(FileExistsError):
        prepare_recovery_tls(source, target, ['another-host'])


@pytest.mark.parametrize('options', [dict(ca=False), dict(signing=False), dict(starts=1),
                                    dict(starts=-60, ends=-1), dict(ends=20)])
def test_unusable_ca_is_never_replaced(tmp_path, options):
    source, target = tmp_path / 'source', tmp_path / 'target'
    authority(source, **options)
    before = (source / 'ca.pem').read_bytes()
    with pytest.raises(certificates.CertificateRecoveryError):
        prepare_recovery_tls(source, target, ['new-host'])
    assert (source / 'ca.pem').read_bytes() == before
    assert not target.exists()


def test_mismatched_or_missing_ca_refused_without_new_identity(tmp_path):
    source, other, target = tmp_path / 'source', tmp_path / 'other', tmp_path / 'target'
    authority(source)
    authority(other)
    (source / 'ca-key.pem').write_bytes((other / 'ca-key.pem').read_bytes())
    with pytest.raises(certificates.CertificateRecoveryError):
        prepare_recovery_tls(source, target, ['new-host'])
    (source / 'ca-key.pem').unlink()
    with pytest.raises(FileNotFoundError):
        prepare_recovery_tls(source, target, ['new-host'])
    assert not target.exists()


@pytest.mark.parametrize('value', ['https://host', '*.local', 'host/other', '-bad', 'bad-',
                                 'bad..local', ' host', 'fe80::1%en0'])
def test_target_names_reject_urls_wildcards_and_scoped_addresses(value):
    with pytest.raises(ValueError):
        target_names([value])


def test_invalid_ca_signature_rejected(tmp_path):
    source = tmp_path / 'source'
    authority(source)
    # A syntactically valid replacement certificate with the same subject/key but
    # signed by an unrelated key must not qualify as a trusted self-signed root.
    cert, key, _, _ = restored_ca(source)
    forged = (x509.CertificateBuilder().subject_name(cert.subject).issuer_name(cert.issuer)
              .public_key(key.public_key()).serial_number(123)
              .not_valid_before(cert.not_valid_before_utc).not_valid_after(cert.not_valid_after_utc)
              .sign(ec.generate_private_key(ec.SECP256R1()), hashes.SHA256()))
    (source / 'ca.pem').write_bytes(certificates._pem_cert(forged))
    with pytest.raises(certificates.CertificateRecoveryError):
        restored_ca(source)


@pytest.mark.parametrize('change', ['ca', 'key', 'leaf'])
def test_tls_reverification_rejects_replaced_material(tmp_path, change):
    source, target, other = tmp_path / 'source', tmp_path / 'target', tmp_path / 'other'
    authority(source)
    authority(other)
    report = prepare_recovery_tls(source, target, ['new-host'])
    if change == 'ca':
        for name in ('ca.pem', 'ca-key.pem'):
            (target / name).write_bytes((other / name).read_bytes())
    elif change == 'key':
        (target / 'server-key.pem').write_bytes((other / 'ca-key.pem').read_bytes())
    else:
        ca, key, _, _ = restored_ca(target)
        certificates._issue_server(target, ca, key, ['new-host'], [])
    with pytest.raises(certificates.CertificateRecoveryError):
        verify_recovery_tls(target, expected_ca_sha256=report['ca_sha256'])
