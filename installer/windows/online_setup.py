"""Online bootstrap entry point. Mac supplies its own UI/adapter."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from installer.common.workflow import read_manifest, prepare
from installer.common.acquire import plain_path
from installer.common.materialize import materialize
from installer.windows.shared_adapter import stage_application


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--descriptor', type=Path, required=True)
    p.add_argument('--attempt', type=Path, required=True)
    p.add_argument('--bundle', type=Path)
    p.add_argument('--prepare-only', action='store_true')
    a = p.parse_args()
    config = json.loads(a.descriptor.read_text(encoding='utf-8-sig'))
    engine = Path(__file__).resolve().parents[2]
    manifest = read_manifest(engine / 'installer-manifest.json', config['manifest_sha256'])
    agreement = engine / manifest.document['agreement_path']
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    choice_file = plain_path(a.attempt / 'choices.json')
    choice_file.parent.mkdir(parents=True, exist_ok=True)
    if not a.prepare_only:
        choice_file.unlink(missing_ok=True)
        subprocess.run([str(shell), '-NoProfile', '-STA', '-File',
                        str(engine / 'installer/windows/online-dialog.ps1'),
                        '-Agreement', str(agreement), '-Choices', str(choice_file)], check=True)
    if not choice_file.exists():
        return
    cancel = a.attempt / 'cancel.requested'
    if not a.prepare_only:
        cancel.unlink(missing_ok=True)
        progress_args = [str(shell), '-NoProfile', '-STA', '-File',
                         str(engine / 'installer/windows/acquisition-dialog.ps1'),
                         '-Python', sys.executable, '-Engine', str(engine),
                         '-Descriptor', str(a.descriptor.absolute()), '-Attempt', str(a.attempt.absolute())]
        if a.bundle:
            progress_args.extend(['-Bundle', str(a.bundle.absolute())])
        subprocess.run(progress_args, check=True)
        if cancel.exists():
            return
        choices = json.loads(choice_file.read_text(encoding='utf-8-sig'))
        subprocess.run([str(shell), '-NoProfile', '-File',
                        str(engine / 'installer/windows/provision-prepared.ps1'),
                        '-Root', str(plain_path(choices['root'])),
                        '-Receipt', str((a.attempt / 'prepared.json').absolute()), '-Elevate',
                        *(['-BackupDirectory', os.environ['FAMILYFOTO_SETUP_BACKUP']]
                          if os.environ.get('FAMILYFOTO_SETUP_BACKUP') else [])], check=True)
        return
    choices = json.loads(choice_file.read_text(encoding='utf-8-sig'))
    root = plain_path(choices['root'])
    if not root.is_absolute() or any(c.isspace() or c == '"' for c in str(root)):
        raise ValueError('Choose a local installation path without spaces or quotes')
    features = ['core'] + (['people'] if choices['people'] else [])
    selected = manifest.plan(features, 'windows-x64')
    if root.exists():
        marker = root / '.shared-installer-release.json'
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')).get('manifest_digest') != manifest.digest:
            raise ValueError('Existing installation folder belongs to another installation; nothing was downloaded')
    if not root.parent.is_dir():
        raise ValueError('Choose an installation folder under an existing parent directory')
    required = sum(item['installed_size_estimate'] for item in selected)
    if shutil.disk_usage(root.parent).free < required:
        raise OSError('Insufficient installation disk space before downloads')
    prepare(manifest, platform='windows-x64', features=features, agreement=agreement.read_bytes(),
            accepted_digest=choices['agreement_digest'], directory=a.attempt,
            mode=config['mode'], bundle=a.bundle, cancelled=cancel.exists, install_root=root)
    receipt_path = a.attempt / 'prepared.json'
    receipt = materialize(manifest, receipt_path, 'windows-x64')
    if cancel.exists():
        raise InterruptedError('Preparation cancelled before application staging')
    stage_application(manifest, receipt, root)


if __name__ == '__main__':
    main()
