"""Explicit offline backup/recovery commands. Never stop or start services."""
import argparse
import json
from pathlib import Path
import sys

from .admission import acknowledge_failed_maintenance, MaintenanceBlocked, maintenance
from .catalog import capture_catalog, verify_catalog
from .postgres import Endpoint
from .recovery import restore_catalog
from .space import InsufficientSpace
from .media_inventory import capture_media_inventory
from .media_tree import copy_media_tree, verify_media_tree
from .library import capture_library, verify_library, verify_library_thumbnails
from .library_restore import restore_library
from .retirement import recover_retirement
from .restic import Restic
from .encrypted_library import capture_encrypted_library, recover_encrypted_publication, restore_encrypted_library
from .upgrade import upgrade_catalog
from ..migration.disk import prepare_disk_move
from ..migration.cutover import commit_disk_move, recover_disk_move
from ..migration.tls import prepare_recovery_tls
from ..migration.credentials import prepare_recovery_role
from ..migration.validation import validate_plain_candidate
from ..migration.preview_access import close_preview_access
from ..migration.preview import preview_library
from ..migration.activation import prepare_activation, ACCEPTANCE_CHECKS
from ..migration.activation_commit import commit_activation
from ..migration.estimate import plan_disk_cutover
from .diagnostics import ExplainedFailure
from .encrypted import read_bounded
from . import passwords


DISK_ACTIONS = {'prepare-disk-move', 'commit-disk-move', 'recover-disk-move'}


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def restic_arguments(command):
    command.add_argument('--restic', type=Path, required=True)
    command.add_argument('--restic-sha256', required=True)
    command.add_argument('--repository', type=Path, required=True)
    command.add_argument('--repository-password-file', type=Path, required=True)


def engine_for(args):
    return Restic(args.restic, args.restic_sha256, args.repository, args.repository_password_file)


# Commands that make a new recovery set. Its password is the administrator password, checked
# against the source's stored hash before the KDF runs (docs/BACKUP.md, "Backup password").
NEW_SET_ACTIONS = {'capture', 'capture-library', 'capture-encrypted', 'upgrade-catalog', 'prepare-disk-move'}


def unlock(root, text):
    return passwords.unlock_context(root, passwords.BackupPassword(text))


def key_for(args, text):
    """The key for this command, from the password text and the right folder's public context."""
    action = args.action
    if action == 'capture-library' and args.resume:
        return unlock(args.directory, text)
    if action == 'prepare-disk-move' and args.resume:
        return unlock(args.backup_directory, text)
    if action == 'commit-disk-move':
        # An earlier attempt that stopped before its intent may have left the job folder's context.
        if passwords.has_context(args.directory):
            return unlock(args.directory, text)
        return passwords.new_key(passwords.administrator_password(args.config_root, text))
    if action in NEW_SET_ACTIONS:
        return passwords.new_key(passwords.administrator_password(args.config_root, text))
    if action == 'recover-encrypted-publication':
        return unlock(Path(args.directory) / 'metadata', text)
    if action == 'commit-activation':
        return unlock(args.intent_directory, text)
    return unlock(args.directory, text)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    actions = result.add_subparsers(dest='action', required=True)
    UNPROTECTED_HELP = 'Accept a disk with no file permissions, such as exFAT, where anyone with the disk can read this output'
    repository = actions.add_parser('restic-init', help='Initialize a new private local encrypted repository')
    restic_arguments(repository)
    repository.add_argument('--allow-unprotected-destination', action='store_true', help=UNPROTECTED_HELP)
    activation = actions.add_parser('commit-activation', help='Explicitly activate a prepared recovery; starts no services')
    activation.add_argument('--intent-directory', type=Path, required=True)
    passwords.add_arguments(activation)
    activation.add_argument('--host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
    activation.add_argument('--port', type=int, required=True)
    activation.add_argument('--username', required=True)
    activation.add_argument('--offline', action='store_true')
    activation.add_argument('--resume', action='store_true')
    activation.add_argument('--rollback', action='store_true')
    activation.add_argument('--reverify-media', action='store_true',
                            help='Read every media file again, instead of trusting the sealed receipt of the last full read')
    thumbnails = actions.add_parser('restore-thumbnails',
                                    help="Put a backup's thumbnails into an activated recovered library")
    for name in ('directory', 'target-directory'):
        thumbnails.add_argument('--' + name, type=Path, required=True)
    passwords.add_arguments(thumbnails)
    cutover_plan = actions.add_parser('plan-disk-cutover', help='Estimate offline rehash work without changing data')
    cutover_plan.add_argument('--directory', type=Path, required=True)
    tls = actions.add_parser('prepare-recovery-tls', help='Preserve restored CA trust and stage a target host certificate')
    tls.add_argument('--source-tls', type=Path, required=True)
    tls.add_argument('--directory', type=Path, required=True)
    tls.add_argument('--name', action='append', required=True)
    role = actions.add_parser('prepare-recovery-role', help='Stage disabled-login credentials on an owned recovery database')
    role.add_argument('--database-record', type=Path, required=True)
    role.add_argument('--expected-cluster-directory', type=Path, required=True)
    role.add_argument('--directory', type=Path, required=True)
    role.add_argument('--host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
    role.add_argument('--port', type=int, required=True)
    role.add_argument('--username', required=True)
    role.add_argument('--resume', action='store_true')
    role.add_argument('--preview', action='store_true', help='Stage SELECT-only preview credentials instead of later edit credentials')
    close_preview = actions.add_parser('close-recovery-preview', help='Revoke an owned recovery preview login after interruption')
    for name in ('database-record', 'directory', 'expected-cluster-directory'):
        close_preview.add_argument('--' + name, type=Path, required=True)
    close_preview.add_argument('--host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
    close_preview.add_argument('--port', type=int, required=True)
    close_preview.add_argument('--username', required=True)
    for action in ('validate-recovery', 'preview-recovery', 'prepare-activation'):
        candidate = actions.add_parser(action, help='Validate or locally preview a recovered library without activation')
        for name in ('directory', 'target-directory', 'role-directory', 'tls-directory', 'expected-cluster-directory'):
            candidate.add_argument('--' + name, type=Path, required=True)
        passwords.add_arguments(candidate)
        candidate.add_argument('--host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
        candidate.add_argument('--port', type=int, required=True)
        candidate.add_argument('--username', required=True)
        candidate.add_argument('--reverify-media', action='store_true', help='Read every media file again, instead of trusting the sealed receipt of the last full read')
        if action == 'preview-recovery':
            candidate.add_argument('--scratch-directory', type=Path, required=True)
            candidate.add_argument('--listen-port', type=int, required=True)
        if action == 'prepare-activation':
            candidate.add_argument('--intent-directory', type=Path, required=True)
            candidate.add_argument('--hostname', required=True)
            candidate.add_argument('--listen-port', type=int, required=True)
            candidate.add_argument('--accepted-check', action='append', choices=sorted(ACCEPTANCE_CHECKS), required=True)
            candidate.add_argument('--source-unavailable', action='store_true',
                                   help='Acknowledge the old source is unavailable and must not be restarted')
    for action in ('verify', 'verify-library'):
        verify = actions.add_parser(action, help='Verify a recovery set without restoring')
        verify.add_argument('--directory', type=Path, required=True)
        passwords.add_arguments(verify)
    ack = actions.add_parser('acknowledge-failed-maintenance', help='Explicitly unblock a failed offline operation; never resumes jobs')
    ack.add_argument('--data-root', type=Path, required=True)
    ack.add_argument('--maintenance-id', required=True)
    receipt = actions.add_parser('recover-retirement', help='Recover an interrupted migration receipt; never activate or restart')
    receipt.add_argument('--data-root', type=Path, required=True)
    receipt.add_argument('--directory', type=Path, required=True)
    passwords.add_arguments(receipt)
    publication = actions.add_parser('recover-encrypted-publication',
                                     help='Finish an interrupted retired-source encrypted capture; never activate or restart')
    restic_arguments(publication)
    publication.add_argument('--repository-id', required=True)
    publication.add_argument('--data-root', type=Path, required=True)
    publication.add_argument('--directory', type=Path, required=True)
    passwords.add_arguments(publication)
    media_verify = actions.add_parser('media-verify', help='Verify a copied media tree without its original disk')
    media_verify.add_argument('--directory', type=Path, required=True)
    for action in ('media-inventory', 'media-copy'):
        command = actions.add_parser(action, help='Offline plain-media operation; not a complete catalog backup')
        command.add_argument('--source-root', type=Path, required=True)
        command.add_argument('--data-root', type=Path, required=True)
        command.add_argument('--directory', type=Path, required=True)
        command.add_argument('--offline', action='store_true')
        if action == 'media-copy':
            command.add_argument('--inventory', type=Path, required=True)
    for action in ('capture', 'capture-library', 'restore', 'restore-library', 'capture-encrypted', 'restore-encrypted',
                   'upgrade-catalog', 'prepare-disk-move', 'commit-disk-move', 'recover-disk-move'):
        command = actions.add_parser(action)
        if action != 'restore-encrypted':
            command.add_argument('--directory', type=Path, required=True, help='New output for capture; existing backup for restore')
        if action.endswith('-encrypted'):
            restic_arguments(command)
            command.add_argument('--repository-id', required=True)
        if action == 'restore-encrypted':
            command.add_argument('--metadata-snapshot', required=True)
            command.add_argument('--metadata-budget-bytes', type=int, required=True)
            command.add_argument('--resume', action='store_true', help='Retry owned incomplete storage restore stages')
        if action in {'restore', 'restore-library'}:
            command.add_argument('--resume', action='store_true', help='Retry an owned validation-only catalog recovery')
        if action == 'restore-library':
            command.add_argument('--in-place', action='store_true',
                                 help="Move the backup's media into the new folder on the same disk instead of copying it. "
                                      'The backup is used up and cannot be restored again.')
            command.add_argument('--media-directory', type=Path,
                                 help="With --in-place: move the media into this new folder on the backup's disk, and keep "
                                      'the target folder (catalog, settings, credentials) on a protected disk.')
            command.add_argument('--allow-unprotected-media', action='store_true',
                                 help='Accept a media folder on a disk that keeps no permissions, such as exFAT. '
                                      'Anyone with the disk can read the photos and their names.')
        passwords.add_arguments(command)
        command.add_argument('--host', choices=('127.0.0.1', 'localhost', '::1'), default='127.0.0.1')
        command.add_argument('--port', type=int, required=True)
        command.add_argument('--username', required=True)
        if action != 'recover-disk-move':
            command.add_argument('--database-budget-bytes', type=int, required=True)
        if action.startswith('capture') or action == 'upgrade-catalog' or action in DISK_ACTIONS:
            command.add_argument('--dbname', required=True)
            if action != 'recover-disk-move':
                command.add_argument('--pg-dump', type=Path, required=True)
            command.add_argument('--config-root', type=Path, required=True)
            command.add_argument('--data-root', type=Path, required=True)
            command.add_argument('--offline', action='store_true', help='Confirm all application and external writers are stopped')
            if action in {'prepare-disk-move', 'commit-disk-move'}:
                command.add_argument('--backup-directory', type=Path, required=True)
            if action == 'commit-disk-move':
                command.add_argument('--accept-offline-rehash', action='store_true')
            if action == 'prepare-disk-move':
                command.add_argument('--resume', action='store_true', help='Resume an owned interrupted preparation')
            if action == 'recover-disk-move':
                command.add_argument('--rollback', action='store_true', help='Undo only an interrupted, still-gated switch')
            if action == 'upgrade-catalog':
                command.add_argument('--migrations-directory', type=Path, required=True)
                command.add_argument('--catalog-only-upgrade', action='store_true',
                                     help='Confirm these upgrades do not change physical media files')
            if action in {'capture-library', 'capture-encrypted'}:
                command.add_argument('--media-root', type=Path, required=True)
                command.add_argument('--retire-to', help='Explicit successor identity; permanently retires this source after capture')
            if action == 'capture-library':
                command.add_argument('--single-pass', action='store_true',
                                     help='Read the library once: take checksums from the catalog and do not read the '
                                          'copies back. Run verify-library, or restore, before relying on the backup')
                command.add_argument('--resume', action='store_true', help='Retry this interrupted capture, rechecking all media')
                command.add_argument('--allow-unprotected-destination', action='store_true', help=UNPROTECTED_HELP)
        else:
            command.add_argument('--pg-restore', type=Path, required=True)
            command.add_argument('--target-directory', type=Path, required=True)
            command.add_argument('--expected-cluster-directory', type=Path, required=True,
                                 help='Data directory of a separately provisioned recovery PostgreSQL cluster')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'restore-thumbnails':
            from .thumbnails import restore_thumbnails
            emit(restore_thumbnails(args.directory, key_for(args, passwords.from_arguments(args)), args.target_directory,
                                    progress=emit))
            return 0
        if args.action == 'plan-disk-cutover':
            emit(plan_disk_cutover(args.directory))
            return 0
        if args.action == 'commit-activation':
            emit(commit_activation(args.intent_directory, key_for(args, passwords.from_arguments(args)),
                 Endpoint(args.host, args.port, 'postgres', args.username), offline_confirmed=args.offline,
                 resume=args.resume, rollback=args.rollback, reverify=args.reverify_media))
            return 0
        if args.action == 'prepare-activation':
            emit(prepare_activation(args.directory, key_for(args, passwords.from_arguments(args)), args.target_directory,
                 Endpoint(args.host, args.port, 'postgres', args.username), args.role_directory, args.tls_directory,
                 args.intent_directory, expected_cluster_directory=args.expected_cluster_directory,
                 accepted=args.accepted_check, source_unavailable=args.source_unavailable,
                 hostname=args.hostname, port=args.listen_port, reverify=args.reverify_media))
            return 0
        if args.action == 'preview-recovery':
            emit(preview_library(args.directory, key_for(args, passwords.from_arguments(args)), args.target_directory,
                 Endpoint(args.host, args.port, 'postgres', args.username), args.role_directory, args.tls_directory,
                 args.scratch_directory, expected_cluster_directory=args.expected_cluster_directory, port=args.listen_port,
                 reverify=args.reverify_media))
            return 0
        if args.action == 'close-recovery-preview':
            record = json.loads(read_bounded(args.database_record, 65536))
            emit(close_preview_access(Endpoint(args.host, args.port, 'postgres', args.username), record,
                 args.directory, expected_cluster_directory=args.expected_cluster_directory))
            return 0
        if args.action == 'validate-recovery':
            emit(validate_plain_candidate(args.directory, key_for(args, passwords.from_arguments(args)), args.target_directory,
                 Endpoint(args.host, args.port, 'postgres', args.username), args.role_directory, args.tls_directory,
                 expected_cluster_directory=args.expected_cluster_directory, reverify=args.reverify_media))
            return 0
        if args.action == 'prepare-recovery-role':
            record = json.loads(read_bounded(args.database_record, 65536))
            emit(prepare_recovery_role(Endpoint(args.host, args.port, 'postgres', args.username),
                 record, args.directory, expected_cluster_directory=args.expected_cluster_directory,
                 resume=args.resume, preview=args.preview))
            return 0
        if args.action == 'prepare-recovery-tls':
            emit(prepare_recovery_tls(args.source_tls, args.directory, args.name))
            return 0
        if args.action == 'restic-init':
            emit({'state': 'initialized', 'repository_id': engine_for(args).initialize(
                allow_unprotected=args.allow_unprotected_destination)})
            return 0
        if args.action == 'acknowledge-failed-maintenance':
            acknowledge_failed_maintenance(args.data_root, args.maintenance_id)
            emit({'state': 'acknowledged', 'jobs_resumed': False})
            return 0
        if args.action == 'media-verify':
            emit({'state': 'verified', **verify_media_tree(args.directory)})
            return 0
        if args.action in {'media-inventory', 'media-copy'}:
            if not args.offline:
                raise ValueError('Confirm all application and external writers are stopped')
            with maintenance(args.data_root, 'backup'):
                if args.action == 'media-inventory':
                    report = capture_media_inventory(args.source_root, args.directory,
                                                     offline_confirmed=True, progress=emit)
                else:
                    report = copy_media_tree(args.source_root, args.inventory, args.directory,
                                             offline_confirmed=True, progress=emit)
            emit({'state': 'complete', 'catalog_included': False, **report})
            return 0
        text = passwords.from_arguments(args)
        if args.action == 'restore-encrypted':
            # Its key is unlocked from the context inside the restored metadata snapshot.
            key = passwords.BackupPassword(text)
        else:
            key = key_for(args, text)
        if args.action == 'recover-retirement':
            report = recover_retirement(args.data_root, args.directory, key)
            emit({'state': 'receipt-verified', 'capture_id': report['capture_id'],
                  'source_retired': True, 'activation_allowed': False})
            return 0
        if args.action == 'recover-encrypted-publication':
            report = recover_encrypted_publication(args.data_root, args.directory, key, engine_for(args),
                                                   args.repository_id, progress=emit)
            emit({'state': 'publication-verified', 'capture_id': report['id'],
                  'metadata_snapshot': report['metadata_snapshot'], 'media_snapshot': report['media_snapshot'],
                  'source_retired': True, 'activation_allowed': False})
            return 0
        if args.action == 'verify':
            report = verify_catalog(args.directory, key)
            emit({'state': 'verified', 'capture_id': report['id'], 'media_included': False, 'restore_tested': False})
            return 0
        if args.action == 'verify-library':
            report = verify_library(args.directory, key)
            emit({'state': 'verified', 'capture_id': report['id'], 'media_included': True,
                  'media_encrypted': False, 'restore_tested': False,
                  'thumbnails_included': verify_library_thumbnails(args.directory, key, report)})
            return 0
        endpoint = Endpoint(args.host, args.port,
                            args.dbname if args.action.startswith('capture') or args.action == 'upgrade-catalog' or args.action in DISK_ACTIONS else 'postgres',
                            args.username)
        if args.action == 'prepare-disk-move':
            report = prepare_disk_move(endpoint, args.pg_dump, args.config_root, args.data_root,
                args.directory, args.backup_directory, key, database_budget=args.database_budget_bytes,
                offline_confirmed=args.offline, progress=emit, resume=args.resume)
            emit({'state': report['state'], 'job_id': report['id'], 'configuration_changed': False,
                  'activation_allowed': False, 'old_media_retained': True})
        elif args.action == 'commit-disk-move':
            if not args.offline:
                raise MaintenanceBlocked('Explicit offline confirmation is required')
            emit(plan_disk_cutover(args.directory))
            if not args.accept_offline_rehash:
                raise ValueError('Review the offline estimate, then explicitly pass --accept-offline-rehash')
            emit(commit_disk_move(endpoint, args.pg_dump, args.config_root, args.data_root,
                args.directory, args.backup_directory, key, database_budget=args.database_budget_bytes,
                offline_confirmed=args.offline, progress=emit))
        elif args.action == 'recover-disk-move':
            emit(recover_disk_move(endpoint, args.config_root, args.data_root, args.directory, key,
                                   rollback=args.rollback, offline_confirmed=args.offline))
        elif args.action == 'upgrade-catalog':
            report = upgrade_catalog(endpoint, args.pg_dump, args.config_root, args.data_root, args.directory, key,
                migrations_directory=args.migrations_directory, database_budget=args.database_budget_bytes,
                offline_confirmed=args.offline, catalog_only_confirmed=args.catalog_only_upgrade, progress=emit)
            emit(report)
        elif args.action == 'capture-encrypted':
            report = capture_encrypted_library(endpoint, args.pg_dump, args.config_root, args.data_root,
                args.media_root, args.directory, key, engine_for(args), args.repository_id,
                database_budget=args.database_budget_bytes, offline_confirmed=args.offline, progress=emit, retire_to=args.retire_to)
            emit({'state': 'complete', **report})
        elif args.action == 'restore-encrypted':
            report = restore_encrypted_library(engine_for(args), args.repository_id, args.metadata_snapshot,
                key, args.target_directory, endpoint, args.pg_restore,
                expected_cluster_directory=args.expected_cluster_directory, database_budget=args.database_budget_bytes,
                metadata_budget=args.metadata_budget_bytes, resume=args.resume, progress=emit)
            emit(report)
        elif args.action == 'capture-library':
            report = capture_library(endpoint, args.pg_dump, args.config_root, args.data_root,
                                     args.media_root, args.directory, key, database_budget=args.database_budget_bytes,
                                     offline_confirmed=args.offline, progress=emit, retire_to=args.retire_to,
                                     resume=args.resume, allow_unprotected=args.allow_unprotected_destination,
                                     single_pass=args.single_pass)
            timing = Path(args.directory) / 'capture-timing.json'
            emit({'state': 'complete', 'capture_id': report['id'], 'media_included': True, 'media_encrypted': False,
                  'media_read_back': report.get('media_read_back', True),
                  'timing': json.loads(read_bounded(timing, 65536)) if timing.is_file() else None})
        elif args.action == 'capture':
            report = capture_catalog(endpoint, args.pg_dump, args.config_root, args.data_root, args.directory, key,
                                     database_budget=args.database_budget_bytes, offline_confirmed=args.offline, progress=emit)
            emit({'state': 'complete', 'capture_id': report['id'], 'media_included': False})
        else:
            restore = restore_library if args.action == 'restore-library' else restore_catalog
            retry = {'resume': args.resume}
            if args.action == 'restore-library':
                retry['in_place'] = args.in_place
                if args.media_directory is not None:
                    retry['media_directory'] = args.media_directory
                if args.allow_unprotected_media:
                    retry['allow_unprotected_media'] = True
            report = restore(args.directory, key, args.target_directory, endpoint, args.pg_restore,
                                     expected_cluster_directory=args.expected_cluster_directory,
                                     database_budget=args.database_budget_bytes, progress=emit, **retry)
            emit(report)
        return 0
    except passwords.PasswordProblem as error:
        emit({'state': 'failed', 'reason': error.reason, 'diagnostic_categories': [], 'message': str(error)})
    except InsufficientSpace as error:
        emit({'state': 'blocked', 'reason': 'insufficient-space', 'capacities': [item.report() for item in error.capacities]})
    except MaintenanceBlocked as error:
        emit({'state': 'blocked', 'reason': 'maintenance', 'message': str(error)})
    except ExplainedFailure as error:
        emit({'state': 'failed', 'reason': 'ValueError', 'diagnostic_categories': [], 'message': str(error)})
    except Exception as error:
        # Database/tool errors may embed credentials or row values. Keep detailed
        # troubleshooting in controlled tests, not the user-facing backup log.
        from .diagnostics import safe_categories
        emit({'state': 'failed', 'reason': type(error).__name__, 'diagnostic_categories': safe_categories(error),
              'message': 'Backup/recovery did not complete. Preserve incomplete work; no activation was performed.'})
    return 1


if __name__ == '__main__':
    sys.exit(main())
