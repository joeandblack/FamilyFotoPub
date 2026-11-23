"""Exercise launcher defaults without downloading code or launching setup."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'installer/macos/handover/restore-familyfoto-mac.command'


@unittest.skipUnless(sys.platform == 'darwin', 'The launcher runs only on macOS')
class LauncherDefaultsTests(unittest.TestCase):
    def test_backup_detection_and_current_directory(self):
        source = SCRIPT.read_text()
        # Run the launcher's actual path/prompt block and final environment handoff.
        block = source[source.index('script_path='):source.index('# Validate the destination')]
        launch = source.splitlines()[-1]
        harness = ('read_path() { printf "%s\\n" "$1"; }\n' + block +
                   '\nprintf "INSTALL=%s\\n" "$install_folder"\n' +
                   'bash() { printf "BACKUP=%s\\n" "$FAMILYFOTO_RESTORE_BACKUP"; }\n' + launch)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            install = root / 'Install Folder'
            install.mkdir()
            backup = root / 'Backup Disk' / 'backup-one'
            (backup / 'catalog').mkdir(parents=True)
            handover = backup / 'restore-on-mac'
            handover.mkdir()
            for folder, expected in [(handover, str(backup)), (root, '')]:
                with self.subTest(folder=folder):
                    result = subprocess.run(['/bin/bash', '-c', harness,
                                             str(folder / SCRIPT.name)], cwd=install,
                                            input='\n', text=True, capture_output=True, check=True)
                    self.assertIn(f'INSTALL={install}\n', result.stdout)
                    self.assertIn(f'BACKUP={expected}\n', result.stdout)


if __name__ == '__main__':
    unittest.main()
