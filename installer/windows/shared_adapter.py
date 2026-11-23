"""Windows adapter: prepare local inputs, then retain the native setup journal."""
import argparse
import os
import subprocess
import sys
from pathlib import Path
from installer.common.acquire import plain_path
from installer.common.materialize import materialize
from installer.common.stage import stage_application as _stage
from installer.common.workflow import read_manifest


def stage_application(manifest, receipt, destination):
    return _stage(manifest, receipt, destination, 'windows-x64')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--manifest-sha256', required=True)
    p.add_argument('--receipt', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--mode', choices=['Install', 'Resume', 'Restore', 'Verify'], required=True)
    a = p.parse_args()
    if sys.platform != 'win32':
        p.error('Windows provisioning requires Windows')
    manifest = read_manifest(a.manifest, a.manifest_sha256)
    receipt = materialize(manifest, a.receipt, 'windows-x64')
    root = plain_path(a.root)
    stage_application(manifest, receipt, root)
    script = root / 'installer/windows/install.ps1'
    if not script.is_file():
        raise ValueError('Verified application release must be staged at --root first')
    # Keep all password input in the existing setup UI/private pipes, never argv.
    env = os.environ.copy()
    env['FAMILYFOTO_PAYLOAD_RECEIPT'] = str(a.receipt.absolute())
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(shell), '-NoProfile', '-File', str(script), '-Mode', a.mode],
                            cwd=root, env=env, check=False)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
