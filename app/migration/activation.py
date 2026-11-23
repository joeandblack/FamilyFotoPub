"""Prepare an authenticated activation intent; never activate while preparing it."""
import base64
import hashlib
import io
import json
from pathlib import Path
import uuid

from ..backup.encrypted import read_bounded, seal_stream
from ..backup.passwords import publish as publish_password
from ..backup.space import SpaceGuard, directory_identity
from ..backup.volumes import volume_identity
from .disk import disk_binding
from .validation import validate_plain_candidate, record
from .candidate_layout import candidate_layout
from ..backup.diagnostics import ExplainedFailure
from ..backup.encrypted_library import verify_metadata
from ..backup.library import verify_library_preflight


FORMAT = 'familyfoto-recovery-activation-intent-v2'
ACCEPTANCE_CHECKS = frozenset({'library', 'albums-collections', 'people', 'recently-deleted',
                              'users-permissions', 'sample-playback'})
# A move to a new computer in one go (app.migration.move_in) replaces the manual review with
# validation and one warning: the person confirms the old computer's FamilyFoto is stopped
# for good. It is never offered on the command line, and only goes with source_unavailable.
SWITCH_WARNING = ('old-library-stopped',)


def acceptance_checks(accepted):
    if not isinstance(accepted, (list, tuple, set, frozenset)) or any(not isinstance(item, str) for item in accepted):
        raise ValueError('Specify the completed recovery acceptance checks')
    if list(accepted) == list(SWITCH_WARNING):
        return list(SWITCH_WARNING)
    if set(accepted) != ACCEPTANCE_CHECKS:
        raise ValueError('Review library, albums/collections, People, Recently Deleted, users/permissions and sample playback')
    return sorted(ACCEPTANCE_CHECKS)


def retirement_claimed(backup, key, directory):
    """Whether the backup says its source was retired, read from its sealed index alone.
    Validation proves the receipt; this only refuses early what it would refuse later."""
    if (backup / 'recovery-point.json').exists():
        job = record(directory / 'restore-job.json')
        return bool(verify_metadata(backup, key, job.get('repository_id')).get('migration_target'))
    return bool(verify_library_preflight(backup, key).get('migration_target'))


def prepare_activation(backup, key, directory, admin, role_directory, tls_directory, output, *,
                       expected_cluster_directory, accepted, source_unavailable=False,
                       hostname, port, probe=disk_binding, guard_factory=SpaceGuard, reverify=False):
    """Seal exact reviewed inputs for a later, separately authorized commit.

    Acceptance is the operator's attestation, not a fabricated automated test.
    Even a prepared intent is not permission to run inference or resume queues.
    Everything that can be refused without reading the media is checked before it is read.
    """
    accepted = acceptance_checks(accepted)
    if type(source_unavailable) is not bool:
        raise ValueError('Source-unavailable acknowledgement must be explicit')
    if accepted == list(SWITCH_WARNING) and source_unavailable is not True:
        raise ValueError('The switch warning confirms the old library is stopped for good, so it goes with source_unavailable')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Choose a non-privileged application port')
    backup, directory, role_directory, tls_directory, output = (
        Path(path).absolute() for path in (backup, directory, role_directory, tls_directory, output))
    cluster = Path(expected_cluster_directory).resolve(strict=True)
    directory_identity(output.parent)
    for protected in (backup, directory, role_directory, tls_directory, cluster):
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError('Activation intent must be separate from all recovery inputs')
    if output.exists() or output.is_symlink():
        raise FileExistsError('Use a new activation-intent directory')
    # Quick refusals first: validation may read the whole library, which takes hours.
    credentials = record(role_directory / 'credentials.json')
    role = record(role_directory / 'ROLE-REPORT.json')
    if credentials.get('access') is not None or role.get('access') is not None:
        raise ValueError('Prepare a separate write-capable NOLOGIN role; preview credentials cannot activate a library')
    tls = record(tls_directory / 'TLS-REPORT.json')
    covered = tls['dns_names'] + tls['ip_addresses']
    if not isinstance(hostname, str) or hostname not in covered:
        raise ExplainedFailure(f'The application hostname {hostname!r} is not in the prepared TLS certificate, '
                               f"which covers {', '.join(covered)}. Names must match exactly, including "
                               'upper and lower case.')
    if not source_unavailable and not retirement_claimed(backup, key, directory):
        raise ValueError('Retire the source or explicitly acknowledge it is unavailable and must not restart')
    validation = validate_plain_candidate(backup, key, directory, admin, role_directory, tls_directory,
                                          expected_cluster_directory=cluster, reverify=reverify)
    if not validation['source_retired'] and not source_unavailable:
        raise ValueError('Retire the source or explicitly acknowledge it is unavailable and must not restart')
    payload, media, inventory, encrypted = candidate_layout(backup, key, directory)
    binding = probe(media)
    if binding.get('root') != str(media) or any(not binding.get(name) for name in ('drive', 'serial', 'volume_serial', 'volume_id')):
        raise ValueError('Target storage has no complete physical identity')
    marker = media.parent / '.familyfoto-volume.json'
    if marker.exists() or marker.is_symlink():
        raise ValueError('Target already has a storage enrollment marker; inspect before activation')
    files = directory / 'catalog/files'
    raw = read_bounded(files / 'config/local.json', 1024 ** 2)
    intent = {'format': FORMAT, 'id': uuid.uuid4().hex, 'validation': validation,
              'accepted_checks': accepted, 'source_unavailable_acknowledged': source_unavailable,
              'backup': str(backup), 'directory': str(directory), 'role_directory': str(role_directory),
              'tls_directory': str(tls_directory), 'cluster_directory': str(cluster),
              'endpoint': [admin.host, admin.port, admin.user],
              'candidate_identity': list(directory_identity(directory)), 'candidate_volume': volume_identity(directory),
              'media_identity': list(directory_identity(media)), 'media_volume': volume_identity(media),
              'configuration_sha256': hashlib.sha256(raw).hexdigest(),
              'original_configuration': base64.b64encode(raw).decode(),
              'database': record(directory / 'catalog/database-target.json'),
              'credentials': credentials, 'role_report': role, 'tls_report': tls,
              'destination': binding, 'hostname': hostname, 'port': port,
              'media_root': str(media), 'inventory_root': str(inventory), 'recovery_payload': payload,
              'encrypted_media': encrypted,
              'inference_enabled': False, 'resume_jobs': False, 'services_started': False}
    encoded = json.dumps(intent, sort_keys=True).encode()
    seal_stream(io.BytesIO(encoded), output, key, estimated_bytes=len(encoded),
                metadata={'kind': FORMAT}, guard_factory=guard_factory)
    # The backup's public password context, so the switch can be unlocked from this folder alone.
    publish_password(output, key)
    return {'state': 'activation-prepared', 'intent_id': intent['id'],
            'library_id': validation['library_id'], 'restoration_id': validation['restoration_id'],
            'source_retired': validation['source_retired'], 'source_unavailable_acknowledged': source_unavailable,
            'activation_allowed': False, 'services_started': False, 'inference_enabled': False, 'resume_jobs': False}
