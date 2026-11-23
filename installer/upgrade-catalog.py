"""Shared Windows/macOS offline catalog upgrade. Default: read-only plan.

Run with the new release's Python environment. This does not replace application
files, install dependencies, stop/start services, or rewrite physical media.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from app.backup.encrypted import read_bounded  # noqa: E402
from app.backup import passwords  # noqa: E402
from app.backup.manage import emit  # noqa: E402
from app.backup.postgres import Endpoint  # noqa: E402
from app.backup.space import directory_identity  # noqa: E402
from app.backup.upgrade import plan_upgrade, upgrade_catalog  # noqa: E402


def endpoint_from_config(root):
    directory_identity(root)
    config = json.loads(read_bounded(root / 'local.json', 1024 ** 2))
    if not isinstance(config, dict) or config.get('recovery', {}).get('activation_required'):
        raise ValueError('A normal source installation configuration is required')
    settings = config['database']
    allowed = {'host', 'port', 'dbname', 'user', 'password'}
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise ValueError('Custom connection options require an explicitly reviewed upgrade procedure')
    return Endpoint(**settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('plan', 'apply'), default='plan')
    parser.add_argument('--config-root', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--backup-directory', type=Path)
    passwords.add_arguments(parser)
    parser.add_argument('--pg-dump', type=Path)
    parser.add_argument('--database-budget-bytes', type=int)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--catalog-only-upgrade', action='store_true')
    args = parser.parse_args(argv)
    try:
        directory_identity(args.data_root)
        endpoint = endpoint_from_config(args.config_root)
        if args.mode == 'plan':
            plan = plan_upgrade(endpoint, ROOT / 'databases')
            emit({**plan, 'mode': 'plan', 'services_changed': False,
                  'media_included': False, 'writes_performed': False})
            return 0
        if not args.offline or not args.catalog_only_upgrade:
            raise ValueError('Offline and catalog-only acknowledgements are required')
        if (not all((args.backup_directory, args.pg_dump))
                or args.database_budget_bytes is None or args.database_budget_bytes <= 0):
            raise ValueError('Explicit backup directory, pg_dump and positive byte budget are required')
        # The pre-upgrade backup is protected by the administrator password, checked against its stored hash.
        key = passwords.new_key(passwords.administrator_password(args.config_root, passwords.from_arguments(args)))
        report = upgrade_catalog(endpoint, args.pg_dump, args.config_root, args.data_root,
            args.backup_directory, key, migrations_directory=ROOT / 'databases',
            database_budget=args.database_budget_bytes, offline_confirmed=args.offline,
            catalog_only_confirmed=args.catalog_only_upgrade, progress=emit)
        emit(report)
        return 0
    except passwords.PasswordProblem as error:
        emit({'state': 'failed', 'reason': error.reason, 'message': str(error)})
        return 1
    except Exception as error:
        # Configuration and driver errors can include credentials. Do not echo them.
        emit({'state': 'failed', 'reason': type(error).__name__,
              'message': 'Upgrade preparation failed. Check explicit paths, permissions and offline requirements. '
                         'No services were restarted. Preserve any recovery set and maintenance journal.'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
