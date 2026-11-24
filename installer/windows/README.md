# Windows Installer

Run from the application repository root:

```powershell
powershell.exe -NoProfile -File installer/windows/install.ps1 -Mode Plan
```

Plan is read-only. Install/Resume require an explicitly chosen fresh installation
and elevated Windows PowerShell 5.1. Never run Install against the live server.

- [Installation guide](WINDOWS_INSTALLATION.md)
- [Step-by-step fresh-install test](TESTING.md)
- [Recorded tests and remaining qualification](TEST_RESULTS.md)
- [Shared installer overview](../README.md)
- [Existing-catalog upgrades: verified backup required](../../docs/BACKUP.md#schema-upgrades)

`-Mode Upgrade` runs a read-only schema plan by default; applying requires explicit
offline acknowledgement and backup inputs. It never starts/stops services or
updates code/dependencies. Do not use fresh Install/Resume to upgrade a catalog.

All Windows setup, launcher provisioning and optional Sandbox tools live here.
Shared Python helpers stay one directory above; runtime launcher targets stay in
`scripts/`. This folder must be distributed with the complete application.

## Independent Python runtime

Fresh setup unpacks the official Python Software Foundation `python` NuGet
package, pinned to CPython 3.12.10 x64, into this installation's `runtime/python`.
No NuGet client is required. The package SHA-256 and the executable/DLL publisher
signatures are checked before an isolated version, prefix and standard-library
probe. The verified runtime is published with a same-volume directory rename;
an existing runtime is never overwritten. Core and People retain separate venvs.

Unlike the former Python EXE installer, this does not register or modify a shared
Python installation, Windows PATH, file associations or launcher. Multiple
FamilyFoto installations can use the same Python version independently. The
explicit `-Python` option still uses an operator-provided interpreter when chosen.
Keep the installation path short and without spaces for Windows PowerShell 5.1.

This is a **fresh-install fix**, not an upgrade of an existing runtime. Completed
installations are unchanged. For an old interrupted setup, do not substitute new
scripts or edit its fingerprint: use a new folder and instance for a new test, or
have its journal inspected before attempting recovery. In particular, clicking
Try again on a setup that failed with the old Python EXE does not apply this fix.

Source: [Python's NuGet distribution](https://docs.python.org/3.12/using/windows.html#the-nuget-org-packages).
Python updates remain a separate reviewed package/version change, not an in-place
NuGet upgrade or a repair of another installation's registered Python.

## The setup window

Run interactively on a fresh folder, `-Mode Install` and `-Mode Restore` open one window,
`setup-dialog.ps1`.

**It asks everything first,** and checks each answer before anything is installed:
- the backup folder;
- whether the key opens that backup (its sealed index is signed with the key);
- the library folder: in place needs the backup's disk, a copy needs the room, and a disk without
  file permissions needs a yes;
- the People terms;
- for a new library, the password. A restore asks none: the restored library brings its own
  accounts, so the empty installation gets a random password nobody sees, as on the Mac. The
  console `-Mode Restore` does the same;
- the installation's own disk: local NTFS, with 10 GB free;
- the installation name and ports, filled in automatically.

**Start** then runs this installer again, unattended, with those answers. The password goes on its
standard input only. The window stays open with each stage, a count, and the full output under
**Show details**. The same output is kept in `logs\setup-dialog-*.log`.

**At the end** it offers **Open FamilyFoto**, and **Trust this computer's certificate** in case the
browser warns. After a stop, **Try again** carries on. A restore checked but not switched on (exit
code 2, which the window's required box prevents) offers **Switch on**.

`-NoDialog` keeps the console questions. A setup that has already begun carries on without the
window.

## Restore from a backup

Release backups name their matching `FamilyFotoSetup.exe` in
`restore-on-windows/README.txt`. Source-install backups include an exact-commit
`restore-on-windows/restore-familyfoto-windows.ps1`. After checking its provenance,
run that launcher from an administrator PowerShell window
under the account that will operate FamilyFoto. Git is required. It downloads
from the public repository over HTTPS (using an existing GitHub CLI login if there is
one, otherwise anonymously); no GitHub account, SSH key or Git configuration change is needed. It prompts for an
installation folder, defaulting to the current folder;
`-InstallDirectory D:\FamilyFotoRestore` supplies it directly. The destination must
be fresh (only the launcher itself may be present), on a fixed local NTFS disk,
without spaces, and have at least 10 GB free. It fetches and checks out only the
backup's stamped commit, verifies HEAD, and
opens the existing restore window, where instance/ports and all restore choices
are checked. It does not restore until you submit that window. Do not use it to
overwrite an existing installation. After interrupted setup, keep that folder and
run its `install.ps1 -Mode Restore` instead. The repository's unstamped launcher
template refuses to run. Backups from locally modified or unidentifiable code
also get a disabled launcher rather than silently choosing a different version.

`-Mode Install` asks whether this is a new library or a restore from a backup.
`-Mode Restore` goes straight to the restore. Either way, Core is installed first, if it isn't
already. Then `installer/windows/restore.py` restores the backup into this installation's own
database and folders, in one go (`app.migration.move_in`, the same engine as the Mac):

```powershell
powershell.exe -NoProfile -File installer/windows/install.ps1 -Mode Restore
```

**Choices.** It asks for anything not given:
- the backup folder, with a picker, and the FamilyFoto administrator password from the old
  computer, as it was when the backup was made. It opens the backup, and afterwards it is the
  administrator's sign-in password. There is no key file;
- in place or a copy, and where to make the library folder;
- whether to install People, after showing the terms.

The folder and the password are checked before anything is recorded: in place needs the folder on
the backup's disk, and a disk without permissions (exFAT) needs a yes. A folder or password that
won't do is asked for again. The setup window checks the password before Start, on a temporary
private Python it prepares when Restore is chosen and removes when it closes
(`setup-password.ps1`): the one thing it makes before Start. A backup made with a recovery key
file isn't supported: restore it with the release that made it.

**The switch warning.** The restore stops there until you type `switch`. Unattended, every choice
is a flag, the warning is confirmed with `-OldLibraryStopped`, and the password comes as one frame
on stdin with `-BackupPasswordFromStdin` (a 4-byte big-endian length, then `{"password": "..."}`
in UTF-8: `app.backup.passwords.frame`). It is checked before Core is installed, and never passed
as an argument:

```powershell
$frame | powershell.exe -NoProfile -File installer/windows/install.ps1 -Mode Restore -WithoutPeople `
  -BackupDirectory F:\FamilyFotoBackup\familyfoto-backup-RUN -BackupPasswordFromStdin `
  -Media copy -MediaFolder "E:\FFLibrary" -OldLibraryStopped
```

Pipe the frame's bytes from a program that writes them to stdin unchanged; Windows PowerShell's
own pipeline turns bytes into text, so `$frame` above stands for such a program, not a variable.

**Carrying on.** Running `-Mode Restore` again asks for the password again: none is kept.

It carries on from the stage that stopped. On Windows,
the database cluster (`data\postgres`) and the setup record (`data\setup`) stay where they are
while the restored library's data moves in around them.

## A second installation on this computer

A computer can hold a second FamilyFoto beside the first, for example to try a restore while the
library in use keeps running. Give it a name and its own ports:

```powershell
powershell.exe -NoProfile -File installer/windows/install.ps1 -Mode Restore `
  -Instance restore-test -DatabasePort 5434 -WebPort 4342 -PeoplePort 54344
```

**What it gets, all its own:**
- a database service, `FamilyFotoPostgres-NAME`;
- a server task, `FamilyFoto Desktop Server (NAME)`;
- a desktop shortcut, `FamilyFoto Server (NAME)`;
- a sign-in cookie, so signing in to one installation doesn't sign you out of the other.

Its folders, Python, FFmpeg, settings, certificate authority and logs are its own anyway.

**How its database is set up.** PostgreSQL's own installer records one PostgreSQL 17 installation
per computer, with an uninstall entry, and the first FamilyFoto owns that record. So a named
installation never runs that installer. It unpacks the same release's plain program files
(checksum-pinned), runs `initdb`, and registers its own service with `pg_ctl register`.

**What stays refused.** An installation without a name keeps the original names and ports (5432,
4340, 54341). It now refuses when PostgreSQL 17 is already installed on the computer, and
suggests `-Instance` instead. A named installation refuses the original ports and any port
already in use. The names and ports are recorded when setup begins: `-Mode Resume` and
`-Mode Restore` keep them.

## Native People

`-Mode Install` asks whether to install People, after showing the face model's
terms (non-commercial research use only), and needs a yes or no: there is no silent
default. The answer is kept in the setup journal, so `-Mode Resume` doesn't ask again.
An unattended install chooses with `-WithPeople -ResearchOnly` (accepting the terms)
or `-WithoutPeople`, and is refused without one. With People, Core is completed first,
then the People step runs. If People fails, Core remains installed; inspect the
People failure separately instead of repeating Core setup. `-Start` starts the
web app only after both steps succeed, never a face scan.

After Core setup, before enrolling media storage or starting the app:

```powershell
powershell.exe -NoProfile -File installer/windows/install-people.ps1 -Mode Plan
# Only after reviewing the non-commercial research model terms:
powershell.exe -NoProfile -File installer/windows/install-people.ps1 -Mode Install -ResearchOnly
powershell.exe -NoProfile -File installer/windows/install-people.ps1 -Mode Verify
```

This optional step installs InsightFace CPU in a separate environment and creates
an empty model-scoped catalog. No Docker, media paths, scans or calibrated policy
are needed or inherited. Automatic face-service startup stays disabled. Choose a
destination during first import, then explicitly start the service in Settings.

The installer verifies that PostgreSQL's data directory belongs to this package
and that both the Library and People state are empty. It refuses live/preview
configurations, external data directories and existing providers. An interrupted
provider installation requires inspection, not a blind overwrite or automatic
rollback of database changes. `Verify` checks this fresh-install state only; it is
not a health checker for an established library.

`-Port` selects a separate People HTTP port (49152-65535, default 54341); later
service starts preserve it. Dependencies and model assets use recorded hashes.
The core installer remains usable without accepting model terms or installing People.

## Restored thumbnails or People jobs report access denied

Windows Restore grants the installation's desktop account access to original
photos and videos as well as its restored
startup lock, thumbnails, previews, face pictures and Discover People jobs before
People setup and server startup. The installer records the step so it can retry
an interrupted repair.

For an installation restored with an earlier release, wait for any active backup
and workers to finish, then stop this installation's FamilyFoto desktop server.
Even an idle server keeps a library lock open. With the updated installer in that installation, run
`installer/windows/install.ps1 -Mode Restore` from an administrator PowerShell
under the original setup account. The restore uses its existing journal and skips
completed stages. For a completed restore, the access repair works with the backup disk offline.
After the repair succeeds, restart FamilyFoto from its desktop shortcut.

The original-media repair has its own `restore-access-media-v2` journal marker,
so a completed restore with the older cache-only repair receives it once on
resume. Failures leave that marker incomplete for retry. The configured media
root must match the restore journal and the recorded destination disk identity.
Only the restored library's media parents and media tree receive the recorded
account's Modify permission; existing ACL entries are preserved. Links and hard
links are refused. Backup files, catalog contents, PostgreSQL files and media
bytes are not changed. An explicitly accepted non-ACL media disk such as exFAT
does not receive an ACL repair.

## Release builder

`build_release.py` assembles and audits a Windows release of the shared installer from local,
already-downloaded inputs (it never downloads or publishes), mirroring
`installer/macos/build_release.py`:

```powershell
python -m installer.windows.build_release assemble --recipe recipe.json --output release
python -m installer.windows.build_release audit --release release
python -m installer.windows.build_release descriptor --release release --engine engine.zip --mode online --notices "..." --output bootstrap-online.json
python -m installer.windows.build_release bundle --release release --engine engine.zip --output offline
```

Build `engine.zip` and `FamilyFotoSetup.exe` with `build_shared.py` from a clean checkout of the
release tag, so the shipped code is exactly that commit. The audit refuses a release whose native
payloads are not the files the setup scripts pin (setup finds prepared payloads by SHA-256 and has
no network fallback), whose locked wheels are missing, which lacks the Mac handover, or whose
copyleft sources are missing. It also refuses a release that ships FFmpeg: the Windows release
does not include it, and `install-ffmpeg.ps1` downloads gyan.dev's pinned build during setup, even
from prepared or offline payloads (`Get-SetupDownload -Network`; FFmpeg is the only such
component). An offline installation therefore needs internet for that one step. The password and
People-view locks are `installer/locks/password-windows-x64.txt` and
`installer/locks/people-view-windows-x64.txt`; the People provider closures are not locked for
Windows yet, so a Windows release offers Core.
