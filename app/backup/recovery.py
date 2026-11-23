"""Restore a catalog set to new storage for validation, never automatic activation."""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

import psycopg

from .catalog import verify_catalog
from .diagnostics import ExplainedFailure
from .encrypted import verify_artifact, read_bounded
from .media_copy import sync_directory, copy_lock
from .private_storage import create_private_directory
from .restore_database import restore_database
from .space import Requirement, SpaceGuard, directory_identity
from .volumes import same_job, volume_identity
from .state_files import restore_state
from .policy import WORKER_STATUSES, PAUSE_MARKERS


def save_private(path, value):
    # An interrupted publication must not block the next write or be mistaken
    # for committed state. Keep abandoned attempts for inspection.
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def abandoned_publication(path, names):
    """Recognize only bounded, unlinked remnants of our named journal writes."""
    if not any(re.fullmatch(re.escape(name) + r'\.[0-9a-f]{32}\.part', path.name) for name in names):
        return False
    info = path.lstat()
    return (not path.is_symlink() and not path.is_junction() and stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1 and info.st_size <= 65536)


def sanitize_database(target, *, ownership_token=None):
    if not re.fullmatch(r'familyfoto_recovery_[0-9a-f]{32}', target.dbname):
        raise ValueError('Sanitation is restricted to newly created recovery databases')
    options = target.connection()
    options['options'] = '-c default_transaction_read_only=off'
    changes = {}
    with psycopg.connect(**options) as db:
        if ownership_token is not None:
            if not re.fullmatch('[0-9a-f]{32}', ownership_token):
                raise ValueError('Invalid recovery database ownership token')
            marker = 'familyfoto-recovery:' + ownership_token
            comment = db.execute("SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                                 "WHERE datname=current_database()").fetchone()[0]
            if comment not in {marker, marker + ':sanitized'}:
                raise ValueError('Recovery database ownership changed before sanitation')
        for table in ('auth_sessions', 'auth_login_limits'):
            if db.execute('SELECT to_regclass(%s)', ('public.' + table,)).fetchone()[0]:
                from psycopg import sql
                changes[table] = db.execute(sql.SQL('DELETE FROM public.{}').format(sql.Identifier(table))).rowcount
        if db.execute("SELECT to_regclass('public.upload_sessions')").fetchone()[0]:
            changes['uploads_paused'] = db.execute("UPDATE public.upload_sessions SET state='paused', "
                "error='Recovered catalog: verify staged files before manually resuming' "
                "WHERE state IN ('uploading','queued','importing','cancelling')").rowcount
        if db.execute("SELECT to_regclass('public.jobs')").fetchone()[0]:
            changes['imports_paused'] = db.execute("UPDATE public.jobs SET state='paused', "
                "error='Recovered catalog: verify storage before manually resuming' "
                "WHERE state IN ('scanning','running','queued')").rowcount
        if ownership_token is not None:
            from psycopg import sql
            db.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(target.dbname), sql.Literal(marker + ':sanitized')))
    return changes


def sanitize_files(files, target, capture_id):
    path = files / 'config/local.json'
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('Restored settings must be an object')
    config['database'] = {'host': target.host, 'port': target.port, 'dbname': target.dbname,
                          'user': 'familyfoto_recovery_disabled',
                          'options': '-c default_transaction_read_only=on'}
    config['auth_secret'] = secrets.token_urlsafe(48)
    config['session_epoch'] = secrets.token_hex(16)
    config.pop('postgres_admin_password', None)
    config['source'] = {}
    config['destination'] = {}
    config['recovery'] = {'state': 'validation-only', 'capture_id': capture_id,
                          'activation_required': True, 'media_verified': False}
    for name in ('preview', 'live_existing_catalog', 'live_native_catalog', 'limited_metadata_edit', 'read_only_preview'):
        config.pop(name, None)
    # Model profiles retain their historical identity but are not started. Paths
    # and runtime compatibility must be rebound/verified before activation.
    save_private(path, config)
    (files / 'data').mkdir(mode=0o700, exist_ok=True)
    save_private(files / 'data/face-service-settings.json', {'start_with_app': False})
    for name in PAUSE_MARKERS:
        with (files / 'data' / name).open('xb') as marker:
            marker.flush()
            os.fsync(marker.fileno())
    sync_directory(files / 'data')
    for name in WORKER_STATUSES:
        status_path = files / 'data' / name
        if not status_path.exists():
            continue
        status = json.loads(status_path.read_text(encoding='utf-8'))
        if not isinstance(status, dict):
            raise ValueError('Restored worker status must be an object')
        status['recovery_requires_review'] = True
        if status.get('state') not in {'paused', 'paused-error', 'complete', 'completed', 'done', 'cancelled', 'error'}:
            status['recovery_previous_state'] = status.get('state')
            status['state'] = 'paused'
        save_private(status_path, status)
    for profile in (files / 'data/face_detect/profiles', files / 'data/compreface/raw-local'):
        for directory in profile.glob('*/jobs/*'):
            save_private(directory / 'control.json', {'action': 'pause'})
            for name in ('raw.json', 'checkpoint.json', 'import.json'):
                path = directory / name
                if not path.exists():
                    continue
                job = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(job, dict) and job.get('state') in {'queued', 'running', 'waiting-service', 'waiting-for-foreground'}:
                    job['state'] = 'paused'
                    save_private(path, job)


def restore_catalog(directory, key, target_directory, admin, pg_restore, *, expected_cluster_directory,
                    database_budget, resume=False, guard_factory=SpaceGuard, progress=None):
    """Use only a separately provisioned recovery cluster and trusted archives."""
    directory = Path(os.path.abspath(directory))
    target_directory = Path(os.path.abspath(target_directory))
    if directory.is_relative_to(target_directory) or target_directory.is_relative_to(directory):
        raise ValueError('Recovery destination must be separate from backup')
    payload = verify_catalog(directory, key)
    state = verify_artifact(directory / 'state', key)
    guard_factory([Requirement('files', target_directory.parent, state['metadata']['file_bytes'] + 1024 ** 2),
                   Requirement('database', Path(expected_cluster_directory), database_budget)]).check()
    if resume:
        directory_identity(target_directory)
    else:
        create_private_directory(target_directory)
    with copy_lock(target_directory):
        expected = {'format': 'familyfoto-catalog-restore-job-v1', 'capture_id': payload['id'],
                    'password': key.binding(),
                    'state_id': state['id'], 'state_sha256': state['sha256'],
                    'identity': list(directory_identity(target_directory)), 'volume': volume_identity(target_directory),
                    'cluster': str(Path(expected_cluster_directory).resolve()),
                    'endpoint': [admin.host, admin.port, admin.user]}
        journal = target_directory / 'catalog-job.json'
        if resume:
            if not journal.exists():
                raise ExplainedFailure('This restore stopped before it recorded any work, so it cannot continue. Nothing was lost: start again in a new folder.')
            if not same_job(json.loads(read_bounded(journal, 65536)), expected, 'identity'):
                raise ValueError('Catalog recovery job identity differs')
            report_path = target_directory / 'RECOVERY-REPORT.json'
            if report_path.exists():
                report = json.loads(read_bounded(report_path, 65536))
                config = json.loads(read_bounded(target_directory / 'files/config/local.json', 1024 ** 2))
                if (report.get('state') != 'validation-only' or report.get('activation_allowed') is not False
                        or config.get('recovery', {}).get('activation_required') is not True):
                    raise ValueError('An activated recovery target cannot be resumed')
        else:
            save_private(journal, expected)
        return _restore_catalog_attempt(directory, key, target_directory, admin, pg_restore, payload,
            expected_cluster_directory=expected_cluster_directory, database_budget=database_budget,
            guard_factory=guard_factory, progress=progress)


def _restore_catalog_attempt(directory, key, target_directory, admin, pg_restore, payload, *,
                             expected_cluster_directory, database_budget, guard_factory, progress):
    marker = target_directory / 'RECOVERY-INCOMPLETE'
    if marker.is_symlink() or marker.is_junction():
        raise ValueError('Linked catalog recovery marker')
    marker.touch()
    report_path = target_directory / 'RECOVERY-REPORT.json'
    if report_path.exists():
        report_path.rename(target_directory / ('previous-report-' + uuid.uuid4().hex + '.json'))
    if progress:
        progress({'phase': 'files'})
    # Never overwrite partially sanitized state. Keep prior attempts for inspection
    # and rebuild this relatively small authoritative state from its verified archive.
    attempt = target_directory / ('files-attempt-' + uuid.uuid4().hex)
    restore_state(directory / 'state', key, attempt, guard_factory=guard_factory)
    files = target_directory / 'files'
    if files.exists():
        directory_identity(files)
        files.rename(target_directory / ('previous-files-' + uuid.uuid4().hex))
    attempt.rename(files)
    sync_directory(target_directory)
    if progress:
        progress({'phase': 'database'})
    database_journal = target_directory / 'database-target.json'
    prior = None
    if database_journal.exists():
        prior = json.loads(read_bounded(database_journal, 65536))
    database = restore_database(directory / 'database', key, admin, pg_restore,
                                expected_cluster_directory=expected_cluster_directory,
                                required_bytes=database_budget, guard_factory=guard_factory,
                                record_target=lambda value: save_private(database_journal, value),
                                resume_record=prior, allow_sanitized=True)
    # Persist the newly created target identity before any sanitation. A failed
    # sanitation retains this record and the incomplete marker for investigation.
    save_private(target_directory / 'database-target.json', database)
    target = replace(admin, dbname=database['database'])
    changes = sanitize_database(target, ownership_token=database['ownership_token'])
    sanitize_files(target_directory / 'files', target, payload['id'])
    report = {'capture_id': payload['id'], 'captured_at': payload['created_at'],
              'database': database['database'], 'state': 'validation-only',
              'activation_allowed': False, 'media_verified': False, 'sanitization': changes,
              'database_default_read_only': True}
    save_private(target_directory / 'RECOVERY-REPORT.json', report)
    marker.unlink()
    sync_directory(target_directory)
    return report
