import json
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
import pytest

from app.backup.encrypted import InvalidArtifact
from app.backup.encrypted_library import capture_encrypted_library, restore_encrypted_library, verify_metadata
from app.backup.postgres import Endpoint
from app.migration.activation import ACCEPTANCE_CHECKS, prepare_activation
from app.migration.activation_commit import commit_activation
from app.migration.credentials import prepare_recovery_role
from app.migration.preview import preview_library
from app.migration.tls import prepare_recovery_tls
from app.migration.validation import validate_plain_candidate
from tests.test_backup_postgres import postgres, ample  # noqa: F401
from tests.test_backup_restic import engine  # noqa: F401
from tests.test_migration_disk import relocation  # noqa: F401
from tests.test_migration_tls import authority
from tests.test_migration_activation_commit import binding


@pytest.mark.parametrize('retire', [False, True])
def test_encrypted_recovery_preview_and_activation(relocation, postgres, engine, tmp_path, retire):
    run, config, data, media, _, _, key = relocation
    params, _, _, _, restore = postgres
    authority(config / 'tls')
    cluster = Path(__file__).resolve().parents[1] / 'temp/backup-plan-pg'
    admin = Endpoint(params['host'], params['port'], 'postgres', params['user'])
    successor = uuid.uuid4().hex if retire else None
    repository = engine.initialize()
    saved = capture_encrypted_library(run.endpoint, run.dump, config, data, media, tmp_path / 'working',
                key, engine, repository, database_budget=1024 ** 2, offline_confirmed=True,
                guard_factory=ample, retire_to=successor)
    target = tmp_path / 'recovered'
    result = restore_encrypted_library(engine, repository, saved['metadata_snapshot'], key, target, admin, restore,
                expected_cluster_directory=cluster, database_budget=1024 ** 2, metadata_budget=4 * 1024 ** 2,
                guard_factory=ample)
    roles = []
    try:
        metadata = target / 'metadata-job/payload'
        index = metadata / 'recovery-point.json'
        original = index.read_bytes()
        modified = json.loads(original)
        modified['media_files'] += 1
        index.write_text(json.dumps(modified), encoding='utf-8')
        try:
            with pytest.raises(ValueError):
                verify_metadata(metadata, key, repository)
        finally:
            index.write_bytes(original)
        database = json.loads((target / 'catalog/database-target.json').read_text())
        tls = tmp_path / 'tls'
        prepare_recovery_tls(target / 'catalog/files/config/tls', tls, ['127.0.0.1'])
        preview_role, edit_role = tmp_path / 'preview-role', tmp_path / 'edit-role'
        for role, preview in ((preview_role, True), (edit_role, False)):
            roles.append(prepare_recovery_role(admin, database, role,
                         expected_cluster_directory=cluster, preview=preview)['role'])
        def launch(cfg):
            assert Path(cfg['destination']['root']) == target / 'media-job/payload'
            with psycopg.connect(**cfg['database']) as db:
                assert db.execute('SELECT count(*) FROM media').fetchone()[0] == 1
        assert preview_library(metadata, key, target, admin, preview_role, tls, tmp_path / 'scratch',
                    expected_cluster_directory=cluster, port=4449, launch=launch)['state'] == 'preview-closed'
        validation = validate_plain_candidate(metadata, key, target, admin, edit_role, tls,
                                              expected_cluster_directory=cluster)
        assert validation['source_retired'] is retire
        if retire:
            assert validation['restoration_id'] == successor
        # A remount changes only the device number. A replaced target changes the inode.
        job = target / 'restore-job.json'
        original = job.read_text()
        for position in (0, 1):
            record = json.loads(original)
            record['target_identity'][position] += 7
            job.write_text(json.dumps(record))
            if position == 0:
                assert validate_plain_candidate(metadata, key, target, admin, edit_role, tls,
                                                expected_cluster_directory=cluster)['source_retired'] is retire
            else:
                with pytest.raises(InvalidArtifact, match='identity'):
                    validate_plain_candidate(metadata, key, target, admin, edit_role, tls,
                                             expected_cluster_directory=cluster)
        job.write_text(original)
        intent = tmp_path / 'activation'
        prepare_activation(metadata, key, target, admin, edit_role, tls, intent,
                    expected_cluster_directory=cluster, accepted=ACCEPTANCE_CHECKS,
                    source_unavailable=not retire, hostname='127.0.0.1', port=4449, probe=binding, guard_factory=ample)
        assert commit_activation(intent, key, admin, offline_confirmed=True,
                                 probe=binding, guard_factory=ample)['state'] == 'activated'
    finally:
        with psycopg.connect(**params, autocommit=True) as db:
            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(result['database'])))
            for role in roles:
                db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
