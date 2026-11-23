"""Explicit standalone capture/verify/restore; never starts or stops services."""
import argparse
import json
from pathlib import Path

from app.backup import passwords
from app.backup.postgres import Endpoint
from .capture import capture_library
from .restore import restore_library, verify
from .diagnostics import describe
from .cleanup import cleanup_incomplete


def parser():
    root = argparse.ArgumentParser(description='FamilyFoto Fast capture, verification, inactive restore and explicit cleanup')
    commands = root.add_subparsers(dest='action', required=True)
    cleanup = commands.add_parser('cleanup-incomplete', help='Preview or explicitly reclaim an owned incomplete fast attempt')
    cleanup.add_argument('--directory', type=Path, required=True)
    passwords.add_arguments(cleanup)
    cleanup.add_argument('--expected-tree', help='Exact fingerprint returned by the preview')
    cleanup.add_argument('--confirm-delete', action='store_true')
    for name in ('capture', 'verify', 'restore'):
        command = commands.add_parser(name)
        command.add_argument('--directory', type=Path, required=True)
        passwords.add_arguments(command)
        if name == 'verify':
            continue
        command.add_argument('--host', default='127.0.0.1')
        command.add_argument('--port', type=int, required=True)
        command.add_argument('--username', required=True)
        command.add_argument('--database-budget-bytes', type=int, required=True)
        if name == 'capture':
            command.add_argument('--dbname', required=True)
            command.add_argument('--pg-dump', type=Path, required=True)
            for folder in ('config-root', 'data-root', 'media-root'):
                command.add_argument('--' + folder, type=Path, required=True)
            command.add_argument('--offline', action='store_true', required=True)
            command.add_argument('--allow-unprotected-destination', action='store_true')
            command.add_argument('--copy-timeout-seconds', type=float, default=7 * 86400)
        else:
            command.add_argument('--pg-restore', type=Path, required=True)
            command.add_argument('--target-directory', type=Path, required=True)
            command.add_argument('--expected-cluster-directory', type=Path, required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    def emit(value):
        print(json.dumps(value, ensure_ascii=True), flush=True)
    try:
        text = passwords.from_arguments(args)
        if args.action == 'capture':
            key = passwords.new_key(passwords.administrator_password(args.config_root, text))
            result = capture_library(Endpoint(args.host, args.port, args.dbname, args.username), args.pg_dump,
                args.config_root, args.data_root, args.media_root, args.directory, key,
                database_budget=args.database_budget_bytes, offline_confirmed=args.offline,
                allow_unprotected=args.allow_unprotected_destination, progress=emit,
                copy_timeout=args.copy_timeout_seconds)
        else:
            key = passwords.unlock_context(args.directory, passwords.BackupPassword(text))
            if args.action == 'cleanup-incomplete':
                result = cleanup_incomplete(args.directory, key, expected_tree=args.expected_tree,
                                            confirm=args.confirm_delete)
            elif args.action == 'verify':
                result = verify(args.directory, key, emit)
            else:
                result = restore_library(args.directory, key, args.target_directory,
                    Endpoint(args.host, args.port, 'postgres', args.username), args.pg_restore,
                    expected_cluster_directory=args.expected_cluster_directory,
                    database_budget=args.database_budget_bytes, progress=emit)
        emit(dict(state='complete', result=result, services_started=False))
        return 0
    except KeyboardInterrupt:
        emit(dict(state='cancelled', incomplete_attempt_retained=True, services_started=False))
        return 130
    except Exception as error:
        # Do not print database credentials, raw subprocess output or private paths.
        emit(dict(state='failed', **describe(error), incomplete_attempt_retained=True,
                  services_started=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
