"""Read-only artifact verification. This never restores or activates a library."""
import argparse
import json
from pathlib import Path

from . import passwords
from .encrypted import verify_artifact


def recovery_set(directory):
    """The nearest folder at or above the artifact that holds the backup's password context."""
    directory = Path(directory).absolute()
    for folder in (directory, *directory.parents):
        if passwords.has_context(folder):
            return folder
    raise passwords.UnsupportedBackup()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify encrypted backup artifact integrity (not a restore drill).")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--backup", type=Path,
                        help="The recovery set holding password.json; otherwise the nearest folder above the artifact")
    passwords.add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        root = args.backup or recovery_set(args.directory)
        key = passwords.unlock_context(root, passwords.BackupPassword(passwords.from_arguments(args)))
        manifest = verify_artifact(args.directory, key)
    except passwords.PasswordProblem as error:
        print(json.dumps({"status": "invalid_or_incomplete", "reason": error.reason, "restore_tested": False}))
        return 1
    except (ValueError, OSError, EOFError):
        print(json.dumps({"status": "invalid_or_incomplete", "restore_tested": False}))
        return 1
    print(json.dumps({"status": "verified", "bytes": manifest["size"], "chunks": manifest["count"],
                      "restore_tested": False, "complete_library_backup": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
