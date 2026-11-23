#!/bin/bash
# Download the Mac installer and launch its native restore window.
set -euo pipefail
umask 077

fail() { printf '\n%s\n' "$*" >&2; exit 1; }

# Accept a pasted path, a quoted path, or a Finder drag-and-drop path.
# shlex parses quoting without executing shell substitutions.
read_path() {
    /opt/homebrew/opt/python@3.12/bin/python3.12 -c '
import os, shlex, sys
value = sys.argv[1].strip()
if value and not os.path.exists(os.path.expanduser(value)):
    try:
        parts = shlex.split(value)
        if len(parts) == 1:
            value = parts[0]
    except ValueError:
        pass
print(os.path.abspath(os.path.expanduser(value)))
' "$1"
}

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || fail 'This installer requires an Apple Silicon Mac.'
[[ "$EUID" != 0 ]] || fail 'Run this as your signed-in account, without sudo.'
[[ -t 0 && -t 1 ]] || fail 'Run this script in Terminal so the installer can open its setup window.'
[[ -x /opt/homebrew/bin/brew ]] || fail 'Install Homebrew first, then run this again.'
[[ -x /opt/homebrew/opt/python@3.12/bin/python3.12 ]] || fail 'Run: /opt/homebrew/bin/brew install python@3.12, then run this again.'
xcrun --find swiftc >/dev/null 2>&1 || fail 'Install the Command Line Tools with xcode-select --install, then run this again.'

script_path="$(read_path "$0")"
starting_folder="$(pwd -P)"
handover_folder="$(cd "$(dirname "$script_path")" && pwd -P)"
backup_folder="$(dirname "$handover_folder")"
default_backup=""
if [[ "$(basename "$handover_folder")" == restore-on-mac && -d "$backup_folder/catalog" ]]; then
    default_backup="$backup_folder"
    printf 'Backup folder: %s\n' "$backup_folder"
fi
printf 'FamilyFoto restore from a PC backup\n\n'
read -r -p "Installation folder [$starting_folder]: " folder_input
install_folder="$(read_path "${folder_input:-$starting_folder}")"

# Validate the destination before downloading anything.
/opt/homebrew/opt/python@3.12/bin/python3.12 -c '
import pathlib, sys
target, launcher = map(pathlib.Path, sys.argv[1:])
if any(p.is_symlink() for p in (target, *target.parents)):
    sys.exit("Choose an installation folder whose path has no symbolic links.")
if target.exists():
    if not target.is_dir():
        sys.exit("The installation path is not a folder.")
    other = [p for p in target.iterdir() if p != launcher and p.name != ".DS_Store"]
    if other:
        sys.exit("The installation folder already contains files. Choose a fresh folder.\n"
                 "For an interrupted restore, run its installer/macos/install.sh restore command.")
' "$install_folder" "$script_path"

staging="$(mktemp -d "${TMPDIR:-/tmp}/familyfoto-download.XXXXXX")"
cleanup() { rm -rf -- "$staging"; }
trap cleanup EXIT
# The branch to install. Change this one name when releases move to another branch.
branch=release/0.1
printf '\nDownloading %s from GitHub…\n' "$branch"
# The public repository downloads over HTTPS; no GitHub account or SSH key is needed.
git clone --depth 1 --single-branch --branch "$branch" \
    https://github.com/joeandblack/FamilyFotoPub.git "$staging/source" || \
    fail 'Download failed. Check this Mac’s network connection, then try again.'

mkdir -p -- "$install_folder"
# Refuse a collision if another process populated the destination during download.
/opt/homebrew/opt/python@3.12/bin/python3.12 -c '
import pathlib, shutil, sys
source, target, launcher = map(pathlib.Path, sys.argv[1:])
other = [p for p in target.iterdir() if p != launcher and p.name != ".DS_Store"]
if other:
    sys.exit("The installation folder changed during download. Choose a fresh folder.")
shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
' "$staging/source" "$install_folder" "$script_path"

printf '\nInstallation folder: %s\n' "$install_folder"
printf '\nIn the restore window, check the preselected PC backup (or Browse if none is selected), enter its administrator password, then choose where to put the library.\n\n'
printf 'Launching the Mac installer…\n'
cd -- "$install_folder"
FAMILYFOTO_RESTORE_BACKUP="$default_backup" bash installer/macos/install.sh restore
