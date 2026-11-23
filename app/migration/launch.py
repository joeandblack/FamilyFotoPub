"""Explicit launcher for an activated recovery, using the installed code/runtime.

Run in a new interpreter: python -m app.migration.launch --directory RECOVERY.
This does not install a service or resume scans. People is view-only until
`app.migration.people_setup` has set this computer up for the library's catalog.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys

from ..backup.encrypted import read_bounded
from ..backup.space import directory_identity


def configure_environment(directory):
    directory = Path(directory).resolve(strict=True)
    files = directory / 'catalog/files'
    for root in (files, files / 'config', files / 'data'):
        directory_identity(root)
    cfg = json.loads(read_bounded(files / 'config/local.json', 1024 ** 2))
    recovery = cfg.get('recovery', {})
    if recovery.get('state') != 'activated' or recovery.get('activation_required') is not False:
        raise ValueError('Complete explicit recovery activation before starting this installation')
    admission = files / 'data/.recovery-admission'
    if any((admission / name).exists() or (admission / name).is_symlink()
           for name in ('maintenance.json', 'retired.json')):
        raise ValueError('Recovery maintenance must finish before starting this installation')
    os.environ.update(FAMILYFOTO_CONFIG_DIR=str(files / 'config'), FAMILYFOTO_DATA_DIR=str(files / 'data'),
                      FAMILYFOTO_LOG_DIR=str(directory / 'logs'), FAMILYFOTO_TEMP_DIR=str(directory / 'temp'))
    return cfg


def editable_people(cfg):
    """People may be edited when this computer is set up for the library's catalog."""
    from face_detect.catalog.certify import write_blocker
    blocker = write_blocker(cfg)
    if blocker:
        print(f'People is view-only: {blocker}. Run python -m app.migration.people_setup to set it up.',
              file=sys.stderr)
    return blocker is None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--repair-thumbnails', action='store_true',
                        help='Rebuild the photo thumbnails and video posters a backup leaves out, then exit without starting the server')
    args = parser.parse_args(argv)
    cfg = configure_environment(args.directory)
    if args.repair_thumbnails:
        from scripts.rebuild_missing_thumbnails import run
        return 0 if not run(apply=True)['failed'] else 1
    from app import api
    restored_people(api.app, cfg, Path(os.environ['FAMILYFOTO_DATA_DIR']), Path(__file__).resolve().parents[2])
    from app.service.desktop import main as serve
    serve()


def restored_people(app, cfg, data_root, root):
    """Serve a recovered library's People: viewable at once, editable once this computer is set up.

    Also used by the Mac service for a library moved into its installation (app.migration.move_in).
    """
    # Existing faces are useful even before the new computer's inference runtime
    # is qualified. Do not launch the archived provider or its old absolute paths.
    from face_detect.api.readonly_people import install
    preview = copy.deepcopy(cfg)
    preview['preview'] = {'data_root': str(data_root), 'people_readonly': True}
    preview['recovery']['activation_required'] = True
    people = Path(data_root) / 'people-preview.json'
    if people.exists() and json.loads(read_bounded(people, 65536)).get('enabled') is True:
        if editable_people(cfg):
            # Set up on this computer (`app.migration.people_setup`): the catalog and a passed
            # library check let People be used as before, not only viewed.
            preview['live_existing_catalog'] = True
            preview['preview']['people_readonly'] = False
        install(app, preview, root)


if __name__ == '__main__':
    raise SystemExit(main())
