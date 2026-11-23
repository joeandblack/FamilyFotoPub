# FamilyFoto on macOS

Existing catalogs: `bash installer/macos/install.sh upgrade` runs a read-only plan.
See [the verified-backup upgrade procedure](../../docs/BACKUP.md#schema-upgrades) before
applying. This command is not a code/dependency updater and never restarts services.

## Supported and tested

- **Supported:** Apple Silicon Macs with **macOS 27 or newer**. Intel Macs and older macOS versions are
  not supported or tested (owner decision, 2026-11-22: no legacy support). Each release states its
  exact minimum, never below 27.0: Homebrew packages are built for the build Mac's macOS, so a
  release built on macOS 27.2 needs 27.2, and its audit refuses any bundled program needing more
  than the release declares.
- **Tested (2026-11-22)** on a clean macOS 27.2 VM with no Homebrew, Command Line Tools or Python
  (`installer/macos/qualify/`): `qualify.sh clean`; Gatekeeper rejecting the quarantined app until
  Open Anyway; the launcher self-check from the offline DMG with no quarantine mark carried over;
  the real python-build-standalone 3.12.16 bootstrap Python; an unattended install with People from
  the release's own files (database, background service, InsightFace and its model) in 16 seconds;
  FamilyFoto running, and signing in from a browser in the VM; every bundled program loading only macOS and
  installation libraries; and an offline install with the network blocked. The Open Anyway and
  setup-window steps by hand, and restores of Mac and Windows backups, are still to be run from
  [qualify/QUALIFICATION.md](qualify/QUALIFICATION.md).
- Terminal installs from a git checkout (below) still use Homebrew's Python 3.12, PostgreSQL 17
  and FFmpeg, and follow the same macOS 27 requirement.

Native Apple Silicon installation with Python 3.12, PostgreSQL 17, and FFmpeg.
The on-demand service supports user-selected source folders, verified imports,
photo thumbnails/edits/trash, video metadata, and destination-only conversion.
Native InsightFace/FaceNet People services are documented in [face_detect](../../face_detect/README.md). Signed/notarized distribution remains separate work.

Before installing, read the [install acceptance note](../../docs/INSTALL_ACCEPTANCE.md): it lists the
third-party software and models the installer downloads, including the face models whose terms limit
them to non-commercial research. `--research-only` means you accepted those terms.

## Prepare a fresh code-only installation

Use a local directory outside iCloud/Desktop/Documents synchronization. Do not
copy another machine's config, database, media, `.venv`, or runtime. Run as the
signed-in user, never sudo. Paths with spaces are supported.

```sh
bash installer/macos/install.sh plan       # Read-only preview
bash installer/macos/install.sh install    # Opens the native setup window
```

The fresh interactive installer opens a SwiftUI window, built with the Mac Command Line Tools.
Choose a new library or a backup, then select **Start**. The window starts FamilyFoto,
shows progress, and offers **Open FamilyFoto**. **Trust this computer’s certificate**
adds its CA to your login keychain if you approve. Details are saved in
`logs/macos-setup.log` (or `.macos-setup.log` if preparation stopped before creating logs).
Run the same command again after an interruption. Reenter the backup's administrator password
when requested; once thumbnails are restored, the remaining stages need neither the backup nor
its password. A partial Core or People installation may need inspection. Use `--no-dialog` for
terminal questions.

Before the window opens, setup creates a temporary private Python environment and installs only
hash-pinned binary password-validation dependencies. It is removed when setup exits; a later launch cleans up marked abandoned runtimes only after their owners have exited. This requires
internet access but does not install Core, create a database or start services. Check password is
an explicit action; ordinary field edits do not run the password KDF. Passwords travel through
private stdin pipes, never arguments, environment variables, setup records or logs.

`install` asks whether to install People, after showing the face model's terms (InsightFace,
non-commercial research use only), and needs a yes or no. An unattended install chooses with
`--with-people --research-only` (accepting the terms) or `--without-people`, and is refused
without one.

Homebrew and its `python@3.12` formula must already be installed at `/opt/homebrew`; their bootstrap is never
run silently. Use current Homebrew for your macOS release. Missing formulae are
installed; Homebrew may update their shared dependencies. App Python libraries
stay in `.venv`. Preparation checks binary wheels and `pip check`.

Preparation and database initialization refuse existing/partial state. Inspect
failures instead of deleting state or re-running installation over a library.
The existing configurator supports `--password-stdin` for deliberate unattended
setup. Do not put credentials in command arguments, logs, or source control.


## Shared installer (in development)

The shared installer (`installer/common/README.md`, "Mac adapter") can provision this Mac
from a release instead of Homebrew: `installer/macos/shared_adapter.py` unpacks the
release's bundled Python 3.12, PostgreSQL 17 and FFmpeg into `bundled/` and runs
`install.sh` with `FAMILYFOTO_PAYLOAD_RECEIPT` set, so every dependency comes from
verified local files. `FamilyFoto Setup.app` (`installer/macos/launcher/`) is the
download that drives it: agreement, installation folder and People choice first, then
downloads with progress, then the release's prebuilt setup window. The steps below, using
Homebrew, are unchanged. Released payloads are not published yet; the release build and
clean-Mac checklist are in [qualify/QUALIFICATION.md](qualify/QUALIFICATION.md).

## Restore from a backup: moving a library to this Mac

On a fresh code-only copy, **Restore from a backup** installs Core and then restores the
library into this installation in one go, ending with FamilyFoto running. Choose it when
`install` asks, or run it directly:

```sh
bash installer/macos/install.sh restore
```

New plain-media backups include a `restore-on-mac` folder with instructions and
`restore-familyfoto-mac.command`. Run the launcher with Bash from Terminal.
It asks for a fresh installation folder (suggesting `~/FamilyFoto-Restore` when
launched from a backup), downloads the `release/0.1` branch of the public repository over HTTPS, and opens
the restore window. The Mac needs internet access and the prerequisites above.
The terminal reports while the setup window is being compiled.

Setup defaults the library location to `FFLibrary` beside the backup folder for both
**Copy** and **In place**. For **Copy**, Browse can choose any folder with room for a
second copy of the media. For **In place**, use Browse to choose another parent folder
on the same disk, outside the backup folder. Setup creates `FFLibrary` there and moves the media with one folder rename. The old-computer-stopped and unprotected-disk acceptance checkboxes
start selected. Review them before selecting **Start**.

It asks for what it needs, with folder pickers and a password field:

1. **The backup folder** the backup page made on the old computer.
2. **The FamilyFoto administrator password from when the backup was made.** Click **Check password**
   before Start. Changing that account's password later does not change existing backups. Setup
   never saves this password. Older key-file backups need their original release.
3. **How to restore the photos and videos.**
   - **In place:** they are moved within the backup disk into a new library folder there, in one
     rename: minutes whatever the size, and no extra space. That disk becomes the library's photo
     disk, and the backup folder is no longer a backup. The way for a large library. On an exFAT
     disk add `--allow-unprotected-media`: anyone with the disk can read the photos.
   - **Copy:** to a new folder on any disk. The backup stays whole. Needs the library's size
     free, and takes as long as the copy.
4. **People**, as for a new library.

The folder is checked as soon as it is chosen, before Core is installed: in place needs it on the
backup's disk, a disk without permissions (exFAT) needs your yes, and the library folder must be
new. A wrong choice is asked again. Then it prints the same restore as one short command, which
asks for the password, for running it again or on another Mac:

```sh
bash installer/macos/install.sh restore -b BACKUP -m copy -f "$HOME/FFLibrary" --without-people
```

`-b`, `-m` and `-f` are short for `--backup`, `--media` and `--media-folder`.
For automation, use `--backup-password-stdin` with one UTF-8 password line through a private pipe.
People stays `--with-people --research-only` (accepting the face model terms) or `--without-people`.

The window asks you to confirm the switch before Start. The terminal restore stops after verification for the same warning:

> **Is your old FamilyFoto still in use?** This makes the restored library the one you edit. If
> the old computer is still running FamilyFoto, changes made there won't appear here, and the two
> copies will drift apart. Stop FamilyFoto on the old computer for good before you continue.

Type `switch` to go on. Pressing Return stops there: the library stays restored and checked but
switched off, and FamilyFoto refuses to start on it. Run the same command again to come back to
the warning. After the switch it puts the thumbnails back, moves the library into this
installation, sets up People if chosen, starts FamilyFoto, and rebuilds any thumbnail or poster
still missing. As for a new library, FamilyFoto doesn't start by itself after the Mac restarts:
open the FamilyFoto app in the installation folder, or see [Launch and stop](#launch-and-stop).

Sign in with the old library's accounts. The certificate keeps the old computer's authority, so
a device that trusted the old computer trusts this one when it uses a name the certificate
covers (`127.0.0.1`, `localhost` and this Mac's `.local` name, or `--tls-name`). Keep the backup
until a new backup of this library has been made and verified.

If anything stops it, the message says why, and running the same command again carries on from
that stage. If it stopped after installing Core but before the restore began, it asks the restore's
questions again and restores into this installation, without installing Core again. The work folder is `restore/`: its `MOVE-IN.json` records the choices and the
finished stages, and the installer's own first settings, certificate and data are kept there,
never deleted. Unattended:

```sh
bash installer/macos/install.sh restore --backup /Volumes/Backup/familyfoto-backup-... \
  --backup-password-stdin --media in-place --media-folder "/Volumes/Backup/FFLibrary" \
  --allow-unprotected-media --with-people --research-only --old-library-stopped
```

How it works, and what each setting becomes: `app/migration/move_in.py` and
`app/migration/settings_rules.py`.

### Read-only preparation checks

`plan` reports host architecture, prerequisite executable availability, existing
installation state and local port availability. It does not read credentials,
start services, download dependencies or write installation files. A blocked socket
probe is reported as unverified, not as a free port. Results are informational;
installation independently rechecks prerequisites and ports.

For an isolated test's proposed ports:

```sh
bash installer/macos/install.sh plan --database-port 55481 --web-port 55482 --people-port 55483
```

Detailed checks use the existing Homebrew Python 3.12. If it is missing, Plan
shows the workflow and prerequisite message without installing it. Port checks
include optional People; Core-only installation does not require that port.

### Fresh native People and isolated testing

`install --with-people --research-only` creates a separate provider environment,
verifies official model checksums, proves ownership of `runtime/postgres`, and
creates an empty model-scoped catalog. It starts only the private database during
provisioning, then stops that child process. No web server, scan, or background
worker is started. Service autostart remains off and matching remains review-only.
Use `--research-only` only after approving the applicable research model terms.

For a fresh test folder (spaces supported), use separate ports:

```sh
bash installer/macos/install.sh install --with-people --research-only \
  --database-port 55481 --web-port 55482 --people-port 55483
```

The administrator password is prompted by default. Automated tests may use
`--password-stdin` through a protected input stream, never a command argument.
`prepare` remains available for dependencies/configuration only, followed by
`manage.py install` for Core initialization. The coordinated workflow locks the
installation directory before changes and refuses existing or partial Core state.

If Core completed but People has not started provisioning, use the standalone
step after inspection instead of rerunning Core:

```sh
.venv/bin/python3 installer/macos/people.py install --research-only --port 54341
```

Existing profiles, People state, or a `runtime/macos-people-install.json` receipt
cause installation to stop. Preserve interrupted state for diagnosis; there is
no automatic cleanup, overwrite, or adoption. The People bootstrap shares the
normal service lock and refuses occupied ports and external data overrides.

Read-only verification of a fresh catalog is available while its own database is
already running:

```sh
.venv/bin/python3 installer/macos/people.py verify
```

Verify does not start the database. A stopped database reports failure rather than
silently starting services. This is a fresh/empty-catalog verifier, not a repair
command for a populated library. Use Settings to start People after first-import
storage setup. Physical disk interruption and reboot tests remain separate.

## Launch and stop

Open `FamilyFoto.app` in the installation folder to start/open the browser.
Open `Stop FamilyFoto.app` to request a safe shutdown. The service runs under the
signed-in user's `launchd`, independent of Terminal/Codex. Its plist is stored in
`runtime`, not `~/Library/LaunchAgents`; no login or boot auto-start is installed.
Keep the repository at its installed path. Local app bundles are unsigned.

CLI actions in `installer/macos/manage.py`: `install`, `start`, `stop`, `status`.
FamilyFoto is two servers, started and stopped together, and the messages name each one:
the **web server** is the address to open, and the **database server** (PostgreSQL) is private
to FamilyFoto, with no web page. Never start the database server by hand (for example with
`pg_ctl ... start`): that starts the database alone, not FamilyFoto.
Settings has a **Backup and recovery** section that can start a backup: it stops only the web server,
keeps the database running, and restarts the web server afterwards
([details](../../docs/BACKUP.md#backing-up-from-settings)). `stop` is refused while a backup runs.
The private cluster is `runtime/postgres`, on **127.0.0.1:5433**, using SCRAM
passwords, no Unix socket, and a restricted application role. The app is on
**https://127.0.0.1:4341**. An occupied port fails closed; the Homebrew default
cluster is never adopted or started by FamilyFoto. `LC_ALL=C` avoids PostgreSQL's
macOS launchd locale startup failure.

Stop closes admission to new operations, requests pauses, drains HTTP requests,
waits for the upload dispatcher and database worker leases, then requests
PostgreSQL smart shutdown. There is no forced process kill. Stop can report
that shutdown remains pending after 60 seconds; it continues waiting safely.
An unresolved decoder lease requires review. Restart does not automatically
resume paused imports; use their Resume controls.

Logs: `logs/macos-service.log`, `logs/macos-postgres.log`, and
`logs/macos-launcher.log`. Configuration is `config/local.json`; generated login
credentials, when requested, are `runtime/administrator-credentials.txt`, both
owner-only and ignored by Git.

## Destination and per-import sources

This Mac is configured for **`/Volumes/SanDisk4T/FamilyFoto/media`**. The marker
is `/Volumes/SanDisk4T/FamilyFoto/.familyfoto-volume.json`. Existing disk contents
are not adopted. The source default is deliberately blank: in Imports, enter an
absolute source folder or click Browse. Each import pins its own source identity.
Browser uploads use staging and do not need a server-side source folder.

The macOS adapter reads diskutil plist metadata and pins both partition UUID and
filesystem UUID. Compatibility fields named `serial`/`volume_serial` contain
those UUIDs; they are not hardware serial numbers. Device numbers are never
used as persistent identity. APFS Data firmlinks are resolved to their real
volume with `df`; stale mount directories, replacement UUIDs, nested-volume
paths, symlinked storage, and read-only destinations fail closed. Supported local
source filesystems are APFS, HFS+, ExFAT, FAT, and NTFS; writable destination
support depends on the mounted filesystem. Network sources are not enabled on
Mac. ExFAT does not support per-file Unix permissions.

Imports copy only, verify SHA-256 before publication, keep source files unchanged,
and reserve at least 5 GiB of destination free space. Rotations/crops are catalog
adjustments. Trash can be restored; permanent deletion removes destination copies
only after the existing checksum/identity checks. Video conversion is enabled on
this Mac with `remove_destination_avi=false`, preserving destination originals.

macOS may request Files and Folders or Removable Volumes access. If access is
denied, grant it to the app/process named by macOS and retry; do not weaken disk
identity validation or substitute another volume with the same name.

## Fresh installer validation, 2026-10-31

See [current test results](TEST_RESULTS.md) for the complete isolated Core/People
run, retained failures and sample inference. Reproduce on an empty test installation
with `.venv/bin/python3 installer/macos/test-native-people.py --run`.

## Validation recorded 2026-10-29

- 123 focused regression tests passed (including the added shutdown-handoff
  check); 9 platform/opt-in tests skipped. Another 27 upload integration tests
  passed in disposable schemas, with 1 opt-in test skipped.
- Native generated JPEG/AVI smoke test passed on APFS source and ExFAT SanDisk:
  actual import/copy hashes, thumbnails, HTTP original/download, rotate/crop,
  trash/restore and deletion of the generated test copy, metadata, AVI-to-MP4
  conversion, and HTTP partial-range playback. Source hashes stayed unchanged.
  The test used a disposable database schema and external test folder, then
  removed those fixtures; no personal photos were scanned.
- Live launchd HTTP checks confirmed the enrolled destination/capacity, blank
  source, login, and removal of the preview-only write restriction.
- Native Stop waited 6.3 seconds for a simulated active worker lease and the
  worker exited normally; both ports were released before restart.
- Missing mounts, same-name replacement UUIDs, read-only volumes, links, and
  shutdown admission are covered by fault-injection tests.
- Physical unplug/replug during writes, logout/reboot/sleep, large imports,
  notarization, automatic upgrades, and interrupted-install recovery are not yet
  qualified. Do not infer these from mocked disk failures.

Run the native smoke test explicitly (creates/removes only its own test folder):

```sh
FAMILYFOTO_MAC_TEST_VOLUME=/Volumes/SanDisk4T .venv/bin/python3 -m pytest tests/test_macos_native_media.py -q
```

Windows startup is unchanged. Shared adapters retain the Windows implementation;
the Mac entry point does not start Docker or face-recognition services.

### Import volume checks

Chunk-level checks use macOS `getattrlist` to read the live volume UUID, mount
point and read-only flag, plus path/device and library-marker checks. They do
not launch `diskutil` or cache a successful UUID result. Full partition/filesystem
identity verification still runs before work and in the background heartbeat.
Missing UUIDs, stale mount directories, changed volumes, nested mounts, linked
paths and read-only destinations stop writes. SHA-256 copy verification is
unchanged. Native ABI definitions follow Apple's `sys/attr.h`:
https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/attr.h
