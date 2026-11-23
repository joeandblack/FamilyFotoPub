# Windows Installation Guide

Setup entry point: `installer/windows/install.ps1` in the application repository.

Status: Core setup, HTTP smoke checks and server stop/start passed in isolated
Windows Sandbox on 2026-10-29. Full VM media-import and reboot qualification remain
required. See [test results](TEST_RESULTS.md). No signed installer package exists yet.
Automatic private Python provisioning is included. Fresh native People is an
optional second step; see section 9 and `install-people.ps1`.
Windows scripts and Sandbox tools are in `installer/windows`; shared Python
configuration/database/verification helpers remain in `installer`. Ship the whole
application tree, including `face_detect`, not this subdirectory alone.
Existing interrupted installations must resume with their original installer
version: the setup fingerprint intentionally rejects a changed script set.

## Quick Setup with the Master Script

Use a fresh application directory on a new installation, not this running server.
Obtain the reviewed source/package files. The script provisions a private CPython
3.12.10 x64 runtime from the official PSF NuGet archive, or accepts an explicit
compatible `-Python` path. Archive provisioning is installation-local and does
not invoke Python's registered EXE/MSI installer or modify another installation.
It verifies the pinned package hash, executable/DLL publisher signatures and an
isolated runtime probe before publishing `runtime/python`. It runs
locally; it is not a remote download-and-execute pipe.

Preview from the application directory, without elevation or any installation:

```powershell
powershell.exe -NoProfile -File .\installer\windows\install.ps1 -Mode Plan
```

Then open **Windows PowerShell 5.1 as administrator using the intended operator's
account**, return to that directory and run:

```powershell
.\installer\windows\install.ps1 -Mode Install
```

Input is limited to one elevation and the hidden administrator password prompt
twice. No source or media destination is requested. The application root is detected
from the install script's location (the parent of `installer`), not the shell's
working directory. Place the fresh package in the desired folder before running it;
code, database cluster and application runtime stay there. The app directory must
be on local NTFS, have 10 GB free and currently contain no spaces.
If `py -3.12` is unavailable, add `-Python C:\Path\To\Python312\python.exe`.
Review dependency terms before installation. If policy blocks a trusted local
script, follow the machine's approved execution policy; never disable it globally.

The master script calls existing task scripts for configuration, PostgreSQL,
database initialization, schema upgrading, FFmpeg and the desktop launcher.
It creates its virtual environment, installs dependencies, downloads the pinned
PostgreSQL installer, verifies hashes, restricts secret-directory ACLs, verifies
database identity/privileges and records step checkpoints. No password is put on
the command line or stored in the checkpoint. The existing private config still
contains generated database credentials; protecting it remains necessary.

After a recoverable interruption, use the same account and unchanged setup files:

```powershell
.\installer\windows\install.ps1 -Mode Resume
```

Resume only applies to installations created by this script. It refuses changed
config/code, another account/folder, completed installations and unsafe partial
native installs. An interrupted PostgreSQL install, config write without a saved
checkpoint, or partially registered launcher needs maintainer review; it is not
automatically erased/reinstalled. FFmpeg extraction failures may likewise need
inspection. This is conservative recovery, not a general repair/upgrader.

Read-only verification works on an existing installation too:

```powershell
powershell.exe -NoProfile -File .\installer\windows\install.ps1 -Mode Verify
```

It checks Core dependencies, database version/role/binding/schema and video tools;
it does not start services, rewrite config, apply upgrades or test original media.
Use **FamilyFoto Server** on the desktop after installation. Add `-Start` to the
Install/Resume command only when you want the web task started after success.
No start-at-login, import, face scan, LAN binding, certificate trust, firewall,
Docker, WSL, GPU-driver or power-policy change is performed.

The sections below are the manual alternative and prerequisite details. Do not
run the manual fresh setup first and then expect the master script to adopt it.

## Before Starting

Use a supported Windows 11 x64 computer. Qualification targets: 4 CPU cores,
16 GB RAM and 10 GB free local SSD space for Core, plus capacity for the library,
database, temporary files and backups. People initially targets 32 GB RAM, an
8 GB NVIDIA GPU, WSL2/Docker and at least 30 GB more local free space. These are
planning budgets, not benchmarked minimums. Core does not require Docker.

Choose one stable Windows operator account. Setup needs administrator approval
for PostgreSQL and any approved system changes; ordinary app use is unelevated.
The existing desktop task requires that operator to remain signed in. Locking the
screen is different from signing out. The server must remain awake and powered;
closing a laptop lid must not suspend it. Do not silently change battery policies.

Do not run this fresh-install procedure over the current working installation.
An existing config, database service, cluster or destination marker requires an
upgrade/restore plan. Do not delete it to get past an installer error.

## 1. Choose Locations

For the existing source layout, choose a dedicated local operator-writable folder,
for example `C:\FamilyFoto`, not Program Files, a network share or a sync folder.
The initial assisted procedure uses a path without spaces; full arbitrary-path
support is a packaging acceptance test. Ensure no other user can modify the app's
code/configuration. Install code and PostgreSQL cluster on a local persistent disk.

At the first import, choose an existing source folder and a new or empty destination folder, for example
`F:\Photos` and `E:\FamilyFoto\media`. These are examples, not actual machine defaults.
They must not overlap. Prefer independent source and destination disks for resilience.
Do not format any disk. The destination must be a local identified Windows volume;
network sources use the app's source-enrollment path. Neither media disk needs to
be connected during installation. Browser uploads need only the server destination.
The destination is saved once and cannot be changed after imports or active uploads
exist without a separate verified migration.

## 2. Obtain Reviewed Application Files

Use a reviewed release/commit, including `installer`, `app`, `web`, `databases`, required `scripts`,
non-secret `config` templates and dependency requirements. Do not copy another
installation's `.venv`, `config/local.json`, `data`, credentials, certificates or
disk identity marker. A future release will provide a signed manifest and verified
artifact cache; that release package does not exist yet.

Open PowerShell as the intended operator and set the working directory:

```powershell
Set-Location C:\FamilyFoto
```

## 3. Install and Verify Python

This section is the manual alternative to the master script's private-runtime step.
Provision a trusted Windows x64 CPython 3.12 runtime. The inspected installation
uses 3.12.14; do not assume every patch has a traditional Windows installer or that
the newest Python minor version is compatible with the pinned wheels. Choose a
verified distribution and security-maintained patch during release qualification.
Use [Python's Windows downloads](https://www.python.org/downloads/windows/), not an
unverified mirror. If the Python launcher is available:

```powershell
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

Use the full path to the chosen interpreter if `py` is unavailable. Activation is
unnecessary. Runtime requirements exclude pytest. For source-checkout testing,
install `requirements-dev.txt` before running pytest. Runtime requirements do not
lock every transitive dependency/hash; this is an assisted source setup, not yet the final
reproducible end-user package. Do not install dependencies into global Python.

## 4. Protect Configuration and Create Credentials

Create the `config` and `temp` directories if absent. Before generating secrets,
restrict their NTFS access to the intended operator, SYSTEM and administrators.
A maintainer should review the exact ACL changes and verify them; Git-ignore does
not protect credentials. Restrict the PostgreSQL installer option file's parent
directory too, because that existing script writes a temporary password there.
Do not apply these ACLs recursively to unrelated folders or media drives.

Then create application configuration without choosing media storage:

```powershell
.\.venv\Scripts\python.exe installer/configure.py
```

Enter and repeat an administrator password of at least 12 characters. The script
stores its hash and generates database/session secrets in private `config/local.json`.
It rejects existing configuration and leaves source/destination unconfigured.
The app can open before storage is selected. Imports validates and enrolls the
destination before the first server import or upload can start. It does not import
files during setup. Do not display or email this JSON file.
Default login name is `family`; default web binding is loopback on port 4340.

## 5. Install PostgreSQL

First check whether the dedicated service or port is already owned:

```powershell
Get-Service FamilyFotoPostgres -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue
```

If either conflicts, stop and resolve ownership/configuration. Do not terminate
another database or point new binaries at an existing cluster.

Follow [Database installation](../../docs/OPERATIONS.md#database) to obtain the pinned PostgreSQL
17.11-5 x64 installer and verify its recorded SHA-256. That pin is the current
script contract, not an assertion of the latest secure version. Requalify a newer
security patch deliberately; changing only the URL would break the hash check.

In administrator PowerShell, from the same app folder:

```powershell
.\installer\windows\install-postgres.ps1 -Installer .\temp\postgresql-17.11-5-windows-x64.exe
```

If execution policy blocks a script, verify its provenance/signature and follow
the machine's approved policy. Do not disable execution policy system-wide.

Back in the operator's PowerShell:

```powershell
.\.venv\Scripts\python.exe installer/init-database.py
```

Administrator PowerShell:

```powershell
Restart-Service FamilyFotoPostgres
Get-NetTCPConnection -LocalPort 5432 -State Listen
```

Confirm it listens only on loopback. Never open 5432 to the LAN. Then, as operator:

```powershell
.\.venv\Scripts\python.exe -m app.database.schema_upgrading
.\runtime\postgres\bin\psql.exe --version
```

This creates the catalog from `databases/*.sql`. It must finish without error.
There are currently 34 numbered SQL files. Existing versions are recorded in the
database; an unchanged schema is not reapplied on every request.

## 6. Install Video Tools

```powershell
.\installer\windows\install-ffmpeg.ps1
.\runtime\ffmpeg\bin\ffmpeg.exe -version
.\runtime\ffmpeg\bin\ffprobe.exe -version
```

The existing script downloads and verifies the pinned FFmpeg 9.0.2 build and places
the tools locally, without modifying global PATH. It refuses an existing tool
directory; use a reviewed upgrade plan instead of deleting working tools.

## 7. Create the Desktop Entry Point

As the intended signed-in operator:

```powershell
.\installer\windows\install-desktop-launcher.ps1
```

Check for an existing task of that name before running: the current helper uses
`-Force` and can replace it. If registration needs elevation, preserve the intended
operator identity rather than registering another administrator's account.

Double-click **FamilyFoto Server** on the desktop, choose **Start Server**, then
**Open FamilyFoto**. Use `https://127.0.0.1:4340/` on the server itself. FamilyFoto serves HTTPS only; the installer creates a private certificate authority in `config\tls\ca.pem`, which each browser computer must trust once.
Do not expose this unencrypted setup to remote browsers. Sign in as `family` using
the password entered earlier. Check `logs/desktop-server.log` if startup fails.

The shortcut starts the complete `app.service.desktop` supervisor. Do not launch
`scripts/start.ps1` alongside it; that older path launches a different web entry
point and does not initialize the People HTTP companion.

## 8. Validate Core Locally

1. Verify login, Albums/Library/Imports pages and normal idle status.
2. Create a small dedicated test source with non-private synthetic photos and one
   short supported video; enroll/use it deliberately, not a large personal archive.
3. Import it; inspect thumbnails, original viewing, video playback and album actions.
4. Confirm the source files remain unchanged and the destination is the intended disk.
5. Pause/complete jobs, stop the web server gracefully, restart and verify persistence.
6. Test unsupported-file reporting and a missing-disk condition without bypassing
   disk checks. Reconnect and use explicit recovery; never rewrite disk IDs by hand.

Do not call this a full installation validation until real clean-machine tests
have covered these steps. An HTTP 200 alone does not verify media or storage safety.

## 9. Optional People Setup

After Core setup, before starting the app or enrolling storage, run:

```powershell
powershell.exe -NoProfile -File installer/windows/install-people.ps1 -Mode Plan
# Explicit approval of the model's non-commercial research terms:
powershell.exe -NoProfile -File installer/windows/install-people.ps1 -Mode Install -ResearchOnly
powershell.exe -NoProfile -File installer/windows/install-people.ps1 -Mode Verify
```

This installs hash-pinned InsightFace CPU dependencies into an isolated provider
environment, verifies model assets and creates an empty model-scoped catalog.
It checks local cluster ownership and refuses existing People state or a populated
library. It does not migrate an existing installation. No media location is asked
for and no matching policy is copied. Service autostart and scans stay off;
automatic matching remains review-only until separately calibrated. A custom
`-Port` is preserved on later service starts. Use Settings to start People after
choosing a destination during first import. No Docker is needed.

See [native validation](../../docs/PEOPLE.md) and the
[fresh-installer constraints](README.md#native-people). If setup stops partway,
inspect the partial state; do not delete databases/configuration to force a retry.

The obsolete container deployment has been removed. Use the native People setup
above; old migration data and backups remain intact.

## 10. Enable Trusted LAN Access

Do this only after local validation, with explicit approval for security changes.

1. Choose a stable hostname/IP, port and certificate SANs for this installation.
2. Generate or obtain a dedicated certificate and protect the leaf/private CA keys.
   Never ship/reuse this workstation's certificate or trust its old fingerprint.
3. Configure `tls.certfile` and `tls.keyfile`; validate chain, names and expiry.
4. With approval, bind to the intended LAN address and permit the app port only
   on the Private profile for the intended local subnet. Do not open DB/People ports.
5. Transfer only the public root certificate through a trusted channel and verify
   its fingerprint before explicitly trusting it on each Windows/Mac client.
6. Visit `https://<chosen-host>:4340/` without warnings; verify login, media and uploads.
   Do not use a browser warning bypass as a setup step. Schedule certificate renewal.

[HTTPS details](../../docs/OPERATIONS.md#https) includes historical deployment facts. Reuse the procedure,
not its old address, expiry dates, keys or machine-specific certificate fingerprint.
No Internet exposure, router port forwarding or public hosting is included here.

## 11. Daily Operation and Recovery

- Keep the operator signed in and the server awake. Screen-off/lock is acceptable;
  sleep, hibernation, shutdown and sign-out interrupt availability. Review power
  and lid behavior explicitly and preserve ventilation; no automatic power edits.
- Start PostgreSQL if necessary, then FamilyFoto. Start the native face service
  in Settings when needed; its app-start preference defaults to disabled.
  Current launcher task has no boot trigger or automatic recovery policy.
- Before maintenance, pause all work, including background Discover people, and
  wait for workers/transfers to stop. **Stop Server stops HTTP, not all workers.**
- Preserve config, the complete PostgreSQL database including recognition schemas,
  People runtime files/face thumbnails, native model manifests and the media disk.
  Keep independent verified backups; a copied live PostgreSQL directory is not a
  substitute for a supported consistent backup.
- Diagnose with private local logs. Redact file paths, names, tokens and credentials
  before sharing a support report. Never fix startup by deleting a cluster or
  removing model catalogs or runtime state.

For upgrades and restores, use [Backup and restore](../../docs/BACKUP.md) (a verified backup is
required before any catalog upgrade) and the [database guide](../../docs/OPERATIONS.md#database). A future installer should automate these
checks; the current master script handles fresh Core setup only, not that complete lifecycle.
