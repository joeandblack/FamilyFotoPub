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



def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ExactCommitTests(unittest.TestCase):
    """The launcher's real download block, run against a local repository instead of GitHub."""

    def download(self, repository, commit):
        source = SCRIPT.read_text()
        block = source[source.index('staging='):source.index('mkdir -p -- "$install_folder"')]
        harness = ('fail() { printf "FAILED: %s\n" "$*"; exit 1; }\n'
                   f'repository={repository}\ncommit={commit}\n' + block +
                   'printf "HEAD=%s\n" "$(git -C "$staging/source" rev-parse HEAD)"\n'
                   'printf "FILE=%s\n" "$(cat "$staging/source/version.txt")"\n')
        return subprocess.run(['/bin/bash', '-c', harness], text=True, capture_output=True)

    def test_downloads_exactly_the_backups_commit_not_the_branch_tip(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'FamilyFotoPub'
            repo.mkdir()
            git('init', '-q', '-b', 'main', cwd=repo)
            for version in ('made the backup', 'moved on since'):
                (repo / 'version.txt').write_text(version)
                git('add', 'version.txt', cwd=repo)
                git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '-m', version, cwd=repo)
            older = git('rev-parse', 'HEAD~1', cwd=repo)
            # Fetching a commit that is not a branch tip, as GitHub allows.
            git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=repo)
            result = self.download('file://' + str(repo), older)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f'HEAD={older}\n', result.stdout)
            self.assertIn('FILE=made the backup\n', result.stdout)
            missing = self.download('file://' + str(repo), '0' * 40)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('FAILED: Could not download FamilyFoto 0000000', missing.stdout)
            self.assertIn('not on GitHub', missing.stdout)

    def test_a_launcher_without_a_commit_refuses_before_asking_anything(self):
        source = SCRIPT.read_text()
        check = next(line for line in source.splitlines() if line.startswith('[[ "$commit" =~'))
        for commit, refused in (("''", True), ('abc', True), ('a' * 40, False)):
            with self.subTest(commit=commit):
                harness = 'fail() { printf "FAILED: %s\n" "$*"; exit 1; }\n' + f'commit={commit}\n' + check + '\necho OK\n'
                result = subprocess.run(['/bin/bash', '-c', harness], text=True, capture_output=True)
                self.assertEqual('FAILED' in result.stdout, refused, result.stdout)
        # The check comes before the installation-folder question.
        self.assertLess(source.index(check), source.index('read -r -p'))


if __name__ == '__main__':
    unittest.main()
