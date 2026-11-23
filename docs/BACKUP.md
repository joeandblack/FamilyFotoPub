# Backup and restore

Backing up a library, proving a backup restores, restoring after a loss, moving a library to
another disk or computer, and protected schema upgrades. Detail on the Fast copy engine's design
and measurements is in the [Fast Backup white paper](../app/fastbackup/WHITE_PAPER.md); restoring onto
a new computer through the installers is in [`installer/`](../installer/README.md).

Contents:
[Goals and decisions](#goals-and-decisions) · [Backup password](#backup-password) ·
[What a recovery point holds](#what-a-recovery-point-holds) · [Backing up from Settings](#backing-up-from-settings) ·
[Standard and Fast](#standard-and-fast) · [Testing a restore](#testing-a-restore) ·
[How a capture works](#how-a-capture-works) · [Restore and activation](#restore-and-activation) ·
[Moving a library](#moving-a-library) · [Schema upgrades](#schema-upgrades) ·
[Writer admission](#writer-admission) · [Formats](#formats) · [Stages and retries](#stages-and-retries) ·
[Command reference](#command-reference) · [Filesystems](#filesystems) · [Security](#security) ·
[Not built yet](#not-built-yet) · [Tests](#tests)

## Goals and decisions

Protect the originals and the work invested in imports, albums, collections, People, manual
decisions, edits and deletion history, on Windows and macOS, including moves between them and
multi-terabyte libraries. Nothing may depend on a drive letter, installation folder,
operating-system user name or CPU.

A backup is a historical copy. A migration changes which installation or disk is active and never
deletes the old copy. A second folder on the same disk is not protection against disk failure. The
one exception is an [in-place restore](#restore-in-place-when-the-disk-cant-hold-two-copies), which
moves a backup's media into the recovered library and so uses the backup up.

In scope: administrator-operated backup and recovery to local or external disks, disk relocation,
and moving to another computer. Out of scope: network or offsite repositories, automatic
destructive restore, two live writers, merging independent libraries, and point-in-time (WAL)
recovery.

| Decision | Why |
|---|---|
| **Offline capture.** Every capture stops the application and all workers, holds a durable maintenance gate and freezes the database for the whole run. | An online capture needs every delete, replace and rename path audited and pinned, which hasn't been done. |
| **Plain media tree is the primary format.** Media is copied as ordinary files under `media-copy/`, openable without FamilyFoto. | Unaided recovery, and shared verification and resume code with disk relocation. |
| **Encrypted restic repository is optional.** A pinned, checksum-verified restic wrapped by FamilyFoto. | Deduplicated history without a custom chunk store. Never exposed as restic arguments; never runs `forget`, `prune` or `unlock` by itself. |
| **Sealed catalog.** The database dump and file-side state are streamed into authenticated chunks, with an encrypted completion manifest written last. | No plaintext dump on disk. A directory without the completion manifest is never a recovery point. |
| **Restore always goes into new storage, a new database cluster and a random new database.** | A dump can run source-supplied code, so restoring is isolated and least-privilege. |
| **A restored copy is validation-only until an administrator activates it.** | Two writable copies of one library is the worst failure. Activation is a separate, journalled step. |
| **Unknown state blocks.** Tables, schemas, files and directories must be classified, or capture refuses. | New data must make a deliberate recovery decision, not silently fall outside the backup. |
| **Fail closed, never rename or repair silently.** Damaged or mismatched material is kept for inspection. | A wrong "fix" in a recovery tool is worse than a refusal. |

## Backup password

A backup is protected by the FamilyFoto **administrator password** (the `browser_user` account)
as it is when the backup is made. There is no key file and no second secret to create or keep. One
password opens the backup and, because the settings come back with it, signs in afterwards.

> Your FamilyFoto administrator password protects this backup's catalog and application data.
> Photos and videos on this backup disk can still be read without it. You will need this same
> password to restore the backup, even if you change it later. FamilyFoto cannot reset it.

- **What it protects.** The database, configuration and application state, thumbnails and recovery
  indexes. Photos and videos in `media-copy` stay ordinary readable files, and public paths and
  sizes stay visible. Don't call this a "fully encrypted backup". A restic repository keeps its own
  separate repository password; never reuse one as the other.
- **Checked on creation.** Every writer (Settings and the `capture`, `capture-library`,
  `capture-encrypted`, `upgrade-catalog`, `prepare-disk-move` and first `commit-disk-move`
  commands) checks the password against the administrator's stored hash in the source's
  `config/local.json` before anything is written. Another account's password is refused, and a
  backup needs an administrator password of at least 8 characters (a long passphrase is
  recommended, since a backup can be attacked offline).
- **Changing it later** applies to the next backup only. Each backup keeps needing the password in
  use when it was made; the backup list shows dates so you can tell which goes with which. A
  restore asks for "the FamilyFoto administrator password from the old computer, as it was when
  this backup was made", and checks that the restored administrator hash accepts it.
- **Key derivation.** `app/backup/passwords.py` turns the password into a Fernet key with scrypt
  (N=2^17, r=8, p=1, 32 bytes, about 128 MiB) and a random 16-byte salt per backup. The account's
  PBKDF2 hash is never key material, and neither the password nor the key is stored. Passwords are
  NFC-normalised UTF-8, spaces are kept, NUL/CR/LF are refused, and input is limited to 1024 bytes.
- **`password.json`** at the recovery set's root is public and bounded (at most 4096 bytes):
  `format` (`familyfoto-password-v1`), `backup_id`, `salt` and `check`, a Fernet token over a fixed
  string binding purpose, format, ID and salt. It's written before any encrypted artifact, reused
  unchanged by retries, and also bound inside the authenticated manifest, so replacing it alone
  fails. A correct check is only a fast unlock; every manifest, chunk and media check still runs.
  Recovery work folders keep a copy, so a journalled restore can continue after the media has moved.
- **Secrets never touch disk.** The password and key live only in UI memory, a local authenticated
  request, private parent-child pipes and the active worker. They never appear in arguments,
  environment variables, URLs, logs, run records, journals or backup contents. Terminals use a
  no-echo prompt; scripts pipe one line to `--backup-password-stdin`. Workers receive one framed
  message (four-byte big-endian length, then at most 8192 bytes of JSON) on their own stdin pipe and
  acknowledge before the server stops; on Windows `install.ps1 -BackupPasswordFromStdin` reads it
  before launching any child, and unrelated children get an empty stdin. A replacement worker after a
  crash asks for the password again.
- **Setup windows** on both platforms prepare a private validation runtime before Start (on Windows
  the pinned PSF NuGet Python; on the Mac an isolated environment with hash-pinned wheels), check the
  password there, and change nothing else before Start. Once a restore has recorded its `thumbnails`
  stage, the remaining stages use local data and need neither the backup nor the password
  (`app/migration/restore_stages.py`).
- **Old key-file backups** can't be read by this version. Restore them with the release that made
  them, and keep them and their keys until a new backup has been restored and checked.

There is no reset, recovery code or bypass. A forgotten password loses the catalog, although the
photos and videos stay readable.

## What a recovery point holds

A recovery point exists only when its top-level index verifies: for the plain format,
`verify-library` succeeding on `library/complete.fernet`; for the encrypted format,
`ENCRYPTED-COMPLETE.json` plus terminal success. A snapshot, a half-written directory or a finished
media copy alone is never one.

```text
<backup>/
  password.json            public password context
  capture-job.json         sealed; binds capture, library, disks and retirement target
  inventory/               SQLite inventory of every physical media file and empty directory
  media-copy/              the media tree, as plain files
  catalog/
    database/              sealed pg_dump (custom format) in chunks
    state/                 sealed file-side state: settings, TLS, People files, uploads
  thumbnails/              sealed copy of thumbnails, posters, face pictures and previews
  library/complete.fernet  sealed index binding catalog, inventory and media
  capture-timing.json      seconds per phase (numbers only)
```

| Component | Treatment |
|---|---|
| PostgreSQL catalog and the People schema | Mandatory, one consistent `pg_dump` snapshot |
| Media, including retained conversion originals, Trash and upload staging | Mandatory; every physical file is inventoried and hashed |
| Settings, account hashes, People decisions and model identity, conversion and upload state, retention records | Mandatory, in the sealed state archive |
| Local TLS CA and key | Mandatory, sealed; restored byte for byte to keep client trust |
| Thumbnails, posters, face pictures, previews | A sealed cache copy; a backup without it is still complete, and `restore-thumbnails` puts it back |
| Logs, generated reports | Excluded |
| Sessions, leases, PIDs, pause signals, worker status, `config/local.json.lock` | Never replayed. Restore issues fresh pause markers and keeps old status as evidence |
| Python environment, PostgreSQL data directory, launchers, binaries | Reinstalled for the target OS, never copied |

The classification is in `app/backup/policy.py` as explicit allowlists, independent of the SQL
files; `tests/test_backup_inventory.py` fails on a new unclassified table or schema. The manifest
authenticates the release, database encoding, locale, extensions, schema-version histories, a hash
of stored function definitions, model and policy fingerprints, row counts, and a logical
`library_id`. Each restore records its own `restoration_id`.

`capture` alone produces only the sealed catalog (useful before upgrades when the media disk
survives) and is never described as a complete backup. `capture-library` and `capture-encrypted`
pair catalog and media.

## Backing up from Settings

Settings has an administrator-only **Backup and recovery** section (`app/backup/admin_api.py`,
`web/backup-settings.mjs`). It shows library and database size, checks a plan, starts a backup and
lists recent runs and restore tests. It runs the same `capture-library` and `verify-library` work as
the [commands](#command-reference).

- **Check this plan** finds everything that can be refused while FamilyFoto is still usable, and
  lists it all at once: the library folder, `pg_dump`, a destination outside the library, data and
  configuration folders with room and private permissions, running imports, conversions or scans,
  and anything with no backup rule (a schema, table, view or file, such as one left behind by tests).
- **Start backup** opens a blocking dialog explaining what happens (keep the computer on and the disk
  connected) and asks for the administrator password. Only then does the server start
  `app.backup.admin_runner` in its own process. The runner stops only the server that started it,
  serves a progress page on the application's address (steps, progress and the run's log), captures
  in one pass, verifies the finished backup, and restarts that server. Only a verified run is called
  verified. The page estimates time from the last verified backup scaled by size, or a range before
  the first one.
- **Failure** leaves the source gated. The page offers **Try again** (which releases only a gate the
  backup took) or **Give up and restart FamilyFoto**. Nothing retries or restarts by itself, and only
  the browser that started the run (via a cookie whose token digest alone is stored) sees details.
- **Staying awake.** On Windows the runner holds `SetThreadExecutionState(ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED)`; on the Mac it runs `caffeinate -i -s -m -w PID`. The display may still sleep.
  Closing a laptop lid, a manual sleep, logout or restart still stop a backup; the log says so if the
  request was refused.
- **If the runner dies** after taking the gate, FamilyFoto can't start. Rerun it with its run ID (the
  file name in `logs/backup-runs/`), in the server's environment:
  `python -m app.backup.admin_runner --run-id RUN_ID`, or release the gate with
  `acknowledge-failed-maintenance`. Run records and logs (`logs/backup-runs/`,
  `logs/backup-runner.log`) are outside the captured folders and hold no secrets.

**Where it works.** Windows: servers started as `app.service.desktop`, `app.service.live` or
`app.migration.launch`, which have the stop signal; others are told to use the command. macOS: the
service's own server (`app.service.macos`, from `FamilyFoto.app` or `manage.py start`). There the
backup writes `runtime/macos-web-pause` and the supervisor stops only the web server, keeping the
private PostgreSQL up, and starts it again afterwards. `manage.py stop`/`start` and the launcher apps
do nothing while a backup runs. If the web server doesn't stop within two minutes the backup doesn't
start. `pg_dump` comes from the tools recorded in `runtime/macos-preparation.json`. A dead runner's
marker is ignored once its process is gone, unless the gate is still held.

**Limits.** Plain backups only (encrypted repositories and retiring a source use the commands). The
whole library is read and copied each time; there's no incremental backup. Size estimates come from
the catalog, and the capture rechecks free space against what it finds. Imports stay paused
afterwards; use their Resume controls.

**Timing example** (Windows, 100,000 synthetic files, 73.4 GB media, 5.6 GB catalog, library and
backup on one NVMe drive): 66 minutes in all, with 20 minutes reading the library, 3 saving the
catalog and settings, 25 copying and 18 checking the finished backup. Copying averaged about 50 MB/s
because small files cost more in opening and flushing than moving data; a single 2 GiB file copied
at over 300 MiB/s. Peak memory stayed under 0.2 GB and the backup used 1.07 times the media's size.
USB hard disks are much slower and haven't been measured.

## Standard and Fast

The Settings copy-method selector has **Standard** and **Fast**. Fast is available on every
installation and selected by default (internally the `fast-full` strategy). New API requests
that omit the method also select Fast. Standard remains an explicit choice. Both
verify every copied file and publish the same authenticated full-library format
(`familyfoto-library-plain-media-v2`), which every restore, migration and activation path reads.
Unknown formats fail closed. The selected method's description shows below the selector.

| | Standard | Fast |
| --- | --- | --- |
| Engine | `app.backup` resumable copy | `app.fastbackup`, one spawned media worker |
| Copy | Streams each file and checkpoints | Copies everything first (Windows: CopyFile2, 16 workers, no overwrite), then verifies in a separate pass |
| Retry | Resumes the interrupted capture | Starts a new attempt folder and keeps the interrupted one |

**Fast in detail.**
- Preparation checks the top-level roots, the saved inventory binding, destination privacy and free
  space (the whole media size plus overhead and reserve) without walking the source again; totals
  come from the captured inventory. Individual names are checked as they're used, so an unsupported
  path length fails during copying, never producing a completed backup.
- During copying, free space and destination identity are rechecked between files every 1 GiB or 30
  seconds. Verification then flushes and hashes every destination file (Windows with short-lived
  handles; POSIX with per-file fsync and, on Darwin, `F_FULLFSYNC`). Read-back uses normal filesystem
  reads, so it may be served from cache: it's a content-integrity check, not a physical-media
  certification.
- **Immutable source contract.** You guarantee nothing modifies the source during the backup. Fast
  builds the inventory once and doesn't rescan the source afterwards, so a file added after inventory
  isn't found and a file changed after its copy isn't compared again. Destination mismatches still
  fail. Destination files aren't locked across phases, so keep other programs out of the backup
  folder too.
- The worker reports a five-second heartbeat; cancellation and a total copy deadline (default seven
  days, `--copy-timeout-seconds`) kill and reap it, and completion is never accepted without a clean
  worker exit. Failures carry safe explanations and OS error codes, never raw paths or tool output.
- Phase timings (preparation, copy, verification, final checks) are logged separately.

**Command line.** From a checkout, with database authentication through `PGPASSFILE`:

```powershell
python -m app.fastbackup capture --offline --config-root C:\example\config --data-root C:\example\data --media-root E:\example-media --directory F:\new-backup --host 127.0.0.1 --port 55400 --dbname example --username example_user --pg-dump C:\tools\pg_dump.exe --database-budget-bytes 10737418240
python -m app.fastbackup verify --directory F:\new-backup
python -m app.fastbackup restore --directory F:\new-backup --target-directory D:\new-inactive-restore --host 127.0.0.1 --port 55401 --username restore_admin --pg-restore C:\tools\pg_restore.exe --expected-cluster-directory D:\disposable-postgres --database-budget-bytes 10737418240
```

The engine lives in `app.fastbackup`, alongside `app.backup`. Use
`python -m app.fastbackup` for all Fast backup commands. There is no top-level
compatibility package.

Capture always uses a new directory (no resume, overwrite, mirror, retirement or automatic cleanup)
and keeps the offline maintenance gate and database locks for the whole run. Add
`--allow-unprotected-destination` for exFAT. Restore delegates to the normal full-verification
restore into a new inactive target.

**Reclaiming an incomplete Fast attempt** is an explicit two-step command:

```text
python -m app.fastbackup cleanup-incomplete --directory <incomplete-attempt-folder>
python -m app.fastbackup cleanup-incomplete --directory <incomplete-attempt-folder> --confirm-delete --expected-tree <fingerprint>
```

The preview shows the capture ID and file and byte counts; confirm with its exact `expected_tree`.
Only payload files under `inventory`, `catalog`, `media-copy`, `library` and `thumbnails` are removed;
the password context, ownership, journals and handover material stay. It supports only attempts
carrying the authenticated `fastbackup-v1` ownership field, refuses completed captures, active work
(it needs the attempt and worker locks), source overlap, moved attempts, substituted disks, links,
reparse points, hard links and unknown entries, and never touches the original library. After an
interruption take a fresh preview. Never reuse a cleaned attempt.

## Testing a restore

**Test a restore** proves a verified backup restores without touching the library, while FamilyFoto
keeps running (`app/backup/admin_restore.py`). In a new private folder it:

1. Checks the backup's sealed catalog and index, then every media file.
2. Creates a temporary PostgreSQL cluster from the same tools, with the source's locale, listening on
   this computer only with a generated password.
3. Runs `restore-library`, stages a disabled login and a new certificate, and runs the same check as
   `validate-recovery`, requiring `activation_allowed` and `services_started` to be false.
4. Stops the temporary cluster, whether or not it worked.

It reads the library four times (the backup at the start, again while copying, each file read back,
and the finished copy under a lock), reusing digests rather than rehashing more. A failed test can be
retried and continues from what's on disk: the cluster comes back on the port recorded in
`library/restore-job.json` (a port taken by another program is reported, never taken), a folder holding
only `copy.lock` is redone, a finished restore isn't repeated but every check runs again, and unfinished
certificates are reissued. A retry still needs the full free space. If the restore process is killed,
the page stops its orphaned cluster when it next notices. The folder holds private settings, so it's
refused on a disk without permissions such as exFAT; delete it when you're done. A backup can't start
while a restore test runs. Only backups whose folder still exists are offered.

## How a capture works

1. **Plan** capacity, destination path lengths, disk identity and private permissions, before any gate.
2. **Take the offline maintenance gate** (`app.backup.admission`). Every enrolled writer holds a
   process lease, so an active participant blocks maintenance, and a crashed maintenance leaves a
   durable marker that blocks participants until an administrator acknowledges it. See
   [Writer admission](#writer-admission).
3. **Freeze the database:** a repeatable-read, read-only transaction exports its snapshot and hands it
   to `pg_dump`. Validation row counts come from the same snapshot.
4. **Hash and copy the media**, then reconcile every catalog reference (including retained originals)
   against the physical inventory. Unfinished deletion intents and uploads that disagree with their
   records block completion. Unreferenced files are included, not discarded.
5. **Seal the state archive**, then publish the index last. Mutation during the copy is detected by
   comparing inventories before and after.
6. **Release the gate, and verify.**

**Copy primitives** (`copy_media_file`, `copy_media_tree`, shared by capture, restore and disk moves):
copies stream in 64 MiB blocks with space and device checks before each block and an atomic checkpoint
after it. A retry rehashes the verified prefix and continues; a changed source, destination or corrupt
prefix blocks resume. Each file is read back before publication, and publication never overwrites (a
no-clobber rename on Windows, a hard link on POSIX, or an exclusive claim on disks without hard links).
Journals pin source and destination directory, file and **volume** identity (Windows volume GUID, macOS
VolumeUUID); device numbers aren't compared across runs because they change on remount. Four states are
never presented as equivalent: copied, read back at write time, whole-tree reread, and restore-tested.

**One pass.** By default a capture reads the library three times (hash for the inventory, copy, read
back), and `verify-library` is a fourth; on a 2.9 TB library that took about 40 hours. `--single-pass`
(what Settings uses) reads it once: the inventory takes sizes and hashes from the catalog (hashing any
file the catalog doesn't list or lists at another size), the copy still hashes every source file and
stops at one that differs, and copies are flushed but not read back. The result is marked
`media_read_back: false` and **is a backup only once every copy has been read against the inventory**,
by `verify-library` or by a restore (which reads every file first). A `--retire-to` capture always
reads its copies before retiring the source.

**Application data resume.** The **Saving application data** stage writes settings, certificates,
durable work and recovery records, upload staging and conversion state. On retry it keeps
`catalog/state` when it has the authenticated `resume.fernet` record: the deterministic stream is
regenerated from the current source, each saved block is authenticated and compared byte for byte,
and only missing, torn, damaged or changed blocks are rewritten (temp file, flush, read back, atomic
replace). A fresh database dump is always taken under a new gate. Older captures without the record
are recaptured once. The record holds only stream identity and chunk size.

**Interruption.** A plain capture resumes with `--resume`, rechecking all media; an encrypted capture
restarts in a new directory and restic reuses what it stored. A failed capture leaves
`maintenance.json` and keeps the source gated until `acknowledge-failed-maintenance`, which never
resumes jobs. Restores resume only into storage and a database they own (an ownership token in the
database comment plus identity records), never adopt a database by name and never drop one. A source
retired for migration stays retired. See [Stages and retries](#stages-and-retries).

**Large libraries.** The first copy is proportional to library bytes (no deduplication in the plain
format, no extra staging copy, memory bounded by streaming and SQLite). Space is checked at preflight
and before every bounded write, with the larger of 1 GiB or 5% headroom. At 100 MB/s one pass takes
about 2.8 hours per TB. Verification detects damage but can't repair it: never overwrite a good backup
with a damaged one, and keep older recovery points.

## Restore and activation

`restore-library` (and `restore-encrypted`) restore a recovery point into new private storage and a
random new database, also when the source disk is gone. The result is validation-only: a
startup-blocking flag, database default read-only, `PUBLIC` connection revoked, sessions and secrets
rotated, old disk bindings and credentials removed, workers paused. A partial restore keeps
`LIBRARY-RESTORE-INCOMPLETE` and never publishes `LIBRARY-RECOVERY-REPORT.json`. Then, in order:

1. `prepare-recovery-role` stages a restricted `NOLOGIN` role on the exact recovery database
   (`--preview` stages a `SELECT`-only one).
2. `prepare-recovery-tls` keeps the restored CA bytes and issues a new leaf for the target names.
3. `validate-recovery` checks the candidate against the authenticated backup, including every restored
   state file, allowing only the intended credential and pause changes.
4. `preview-recovery` opens the UI read-only on `127.0.0.1` with HTTPS, a `SELECT`-only role, in-memory
   sessions and a 55-minute limit. `close-recovery-preview` revokes a login a killed process left.
5. `prepare-activation` seals an activation intent once the administrator names the checks they
   accepted. A migration needs the source's retirement receipt; disaster recovery without one needs
   `--source-unavailable`.
6. `commit-activation` journals the configuration switch and enables the staged login. It starts no
   service and resumes no job.

Restoring is trusted-archive work: a dump can execute source-supplied code, so restore only verified
recovery points, into the isolated recovery cluster. Installers wrap these steps: the Mac installer's
**Restore from a backup** runs them in one go and ends with FamilyFoto running
([installer/macos](../installer/macos/README.md#restore-from-a-backup-moving-a-library-to-this-mac)).
The manual steps are in [Recover a lost library](#recover-a-lost-library-step-by-step).

## Moving a library

**Same computer, another disk.** `prepare-disk-move` holds offline admission, captures the catalog,
makes one verified media copy on the new disk, reconciles references and pins configuration hash,
library ID, marker and disk identities. `commit-disk-move` captures a fresh catalog, reconciles both
copies, journals old and new configuration, enrols the destination marker and replaces the settings
atomically; only the destination changes. An interrupted commit keeps the gate and can finish or roll
back (`recover-disk-move`) while that same gate still excludes writers; once released, rollback is
refused. `plan-disk-cutover` estimates the rehash downtime. The old disk is never deleted.

**Another computer, same or different OS.** Capture with `--retire-to` (plain or encrypted), which
permanently retires the source and binds an encrypted receipt to the capture, library and successor
IDs. Install the target's Python, PostgreSQL, FFmpeg and models, then restore, validate, preview and
activate. A model that can't run on the target leaves People read-only until it is set up there (see
[People](PEOPLE.md#moving-to-another-computer)). Retiring is an expert command, not an end-user button.

**Single writer.** Writers are blocked by the retirement marker, which is never restored onto a
successor. A local lock can't stop a second computer writing a shared disk, so the handoff is also
procedural: retire the source and take it offline before activating the target. Rolling back after the
target has been written needs reconciliation or a new recovery point.

## Schema upgrades

An existing catalog is **never upgraded without a freshly verified catalog backup**, and there is no
override: a full disk, disconnected backup disk, failed dump or failed verification blocks the upgrade.
Fresh empty databases and already-current schemas need no backup. Upgrades that modify physical media
are not supported here. This is a catalog upgrade, not a code updater: install the new release and its
dependencies first, stop the app and all workers, and use the new release's Python.

1. Read-only plan: `python installer/upgrade-catalog.py --mode plan --config-root <config> --data-root <data>`
2. Have the administrator password ready; it protects the new backup.
3. Apply into a **new** backup directory, with a conservative dump budget:

   ```text
   python installer/upgrade-catalog.py --mode apply --config-root <config> --data-root <data> --backup-directory <new-backup> --pg-dump <pg_dump> --database-budget-bytes <bytes> --offline --catalog-only-upgrade
   ```

4. Only a `complete` (or already-current `unchanged`) result permits restarting. An interrupted
   operation stays gated; keep the backup and journal, inspect them, then acknowledge. Acknowledging
   never authorises an unprotected upgrade.

The installers expose the same coordinator without starting services: Windows
`installer/windows/install.ps1 -Mode Upgrade` (add `-BackupDirectory`, `-PgDump`, `-DatabaseBudgetBytes`,
`-Offline`, `-CatalogOnlyUpgrade` to apply; `-BackupPasswordFromStdin` for a framed password), and Mac
`bash installer/macos/install.sh upgrade` plus the shared `--mode apply` arguments. Ordinary startup
refuses pending upgrades on an existing catalog; never edit schema history to bypass this.
`scripts/consolidate_import_membership.py --apply` is refused for the same reason.

## Writer admission

Captures require **all application processes and external database or maintenance clients stopped**.
The admission lease is a second line of defence against stray application code, not a sandbox against
an administrator or a script connecting directly to PostgreSQL.

- Configuration loading takes the process lease before creating runtime paths. Both shared database
  helpers take it even with explicit connection settings, so People callers can't bypass it.
- Desktop launch, HEIF workers, native-provider setup, certificate and JSON state writers, credential
  upgrades, fresh installer configuration and database bootstrap all enrol before their first write.
- Recovery and upgrade coordinators use their own exclusive maintenance gate and explicitly bound
  connections, and never take a normal writer lease inside it.
- `tests/test_backup_writer_inventory.py` enumerates production entry points, scripts and launchers;
  an unclassified new one fails the test. A static inventory can't prove there's no earlier write, so
  review a changed entry point's first side effect, and never classify a writer as read-only just to
  pass the test.
- Original and recovered installations must never run as two writable copies. Source retirement and
  the activation write-access fence are separate protections; clearing a failed-maintenance marker
  undoes neither.

## Formats

**Sealed artifacts** (`familyfoto-fernet-stream-v1`). Fernet (AES-128-CBC with HMAC-SHA256) per 4 MiB
chunk, stored as binary envelopes (base64-url-encode them to use `Fernet.decrypt` directly). Chunks are
`00000000.fernet` onward, each payload prefixed by a 16-byte artifact UUID and an 8-byte big-endian
index so reordered or foreign chunks are refused. `complete.fernet` is an encrypted manifest (artifact
UUID, length, chunk size and count, whole-stream SHA-256, at most 32 KiB metadata) written last, after
read-back. One stream is limited to 4 TiB. Verification authenticates everything and extracts no paths.
Anyone with the key can read and forge artifacts, and the format doesn't stop deletion or substitution
of an older valid backup; choose recovery points deliberately. Timestamps, chunk counts and approximate
sizes are visible. The state payload is `familyfoto-state-tar-v1`.

**Database.** `pg_dump` custom format from the exported snapshot, preserving database-local schema,
data, functions, sequences and the People schema (not cluster roles, tablespaces or settings files).
Restored with `pg_restore --single-transaction --exit-on-error --no-owner --no-privileges` into a new
database of a separate loopback cluster, checking the real `template0` locale, major version, extensions,
schema histories, function hashes, model fingerprints and exact row counts. Cross-locale or cross-major
restores are refused. A restored function is never replaced from current source.

**Plain media.** Unencrypted by design and must sit on a disk you control. Keys, credentials and
restored application state are written only where `create_private_directory` proves owner-only
permissions persist (two different modes must read back; Windows ACLs must stick). Capturing to an
unprotected disk needs `--allow-unprotected-destination`, leaving media and names readable.

**Encrypted restic repository.** Restic 0.19.1, pinned by SHA-256 and rechecked before every call
(Windows: `restic_0.19.1_windows_amd64.zip`, archive
`da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d`, executable
`b0dd1fd21eea5d8fe1325f55f7118213c21f36de8a261e04c0624a5ab9fd7830`, from
https://github.com/restic/restic/releases/tag/v0.19.1). Only local repositories; initialisation needs a
new private directory; the repository password comes from a separate file; inherited `RESTIC_*`
settings are removed; cache is disabled. Backup requires the full repository ID and a capture ID, forces
content reads, refuses every nonzero exit including partial snapshots, and runs a full read check.
Restore accepts only a full snapshot ID into a new target with read-back verification. Media streams
straight into restic; a second snapshot holds the sealed catalog, inventory and library index, both
tagged with one capture identity. On macOS, Homebrew's `restic` symlink is refused; pass the real
executable and its hash. Failed snapshots are kept, never pruned or unlocked automatically.

## Stages and retries

**Capture.** `capture-library --resume` retries a plain capture in its directory after authenticating
`capture-job.json` (capture and library IDs, retirement target, paths, identities and volumes of all
roots; refusal takes no new gate). `capture-encrypted` has no resume.

| Stage | Durable evidence | After interruption |
| --- | --- | --- |
| Media inventory | `INVENTORY-INCOMPLETE`, SQLite inventory | Plain retry rereads a complete inventory against the source, else sets it aside and restarts. Encrypted restarts |
| Database dump | Sealed artifact, `complete.fernet` last | Restart under a new freeze; the old one is kept as `previous-catalog-*` |
| State archive | `resume.fernet`, encrypted blocks, `complete.fernet` last | Plain retry keeps matching blocks and rewrites only missing, changed or damaged ones |
| Plain media copy | `copy-job.json`, `TREE-INCOMPLETE`, per-file `copy.json` | Resumes through verified prefixes and rehashes finished files |
| Encrypted media snapshot | Restic snapshot, then `check --read-data` | Restart; an unreferenced snapshot may remain and is never a recovery point |
| Catalog and library index | Sealed `catalog`, `library` / `library-index` | Restart; an unfinished index is set aside, a sealed plain index refuses a retry |
| Source retirement | `.recovery-admission/retired.json`, created exclusively | Permanent; blocks writers and maintenance |
| Retirement receipt | Sealed `retirement` artifact | `recover-retirement` (plain), `recover-encrypted-publication` (encrypted) |
| Encrypted metadata snapshot and completion | Restic snapshot; `ENCRYPTED-COMPLETE.json` | `recover-encrypted-publication` if the source was retired, else restart |

**Restore.** Both restores resume only with `--resume`, into a target marked `LIBRARY-RESTORE-INCOMPLETE`,
bound to target identity, volume, cluster and database endpoint. Encrypted snapshot jobs reuse a stage
marked `VERIFIED` without `INCOMPLETE`; catalog files are rebuilt from the verified archive each time;
the database resumes only into the one whose comment holds its ownership token; a completed target
refuses resume. Across runs, a target reconnected at its original path is accepted by volume ID and inode;
a different disk, or the same disk at another path, is refused.

| Message | Meaning | Action |
| --- | --- | --- |
| `Previous maintenance requires explicit recovery` | A failed operation left the gate | Inspect, `acknowledge-failed-maintenance`, then `--resume` (plain) or a new directory |
| `Media changed since the interrupted capture` | The source differs from its inventory | Acknowledge, then capture into a new directory |
| `Capture source, destination disk or retirement target changed` | Arguments, disks or mount paths differ | Correct them or remount at the original path; a different disk needs a new directory |
| `This directory has no retry record` | Too old, or stopped before its record was saved | Capture into a new directory |
| `This capture already finished` | The directory holds its recovery point | Run `verify-library`. If a migration capture stopped before retiring its source, capture again to retire it |
| `Unowned capture directory` | Something a capture doesn't create is present | Review it; don't delete it just to retry |
| `This library installation is retired` | A migration capture retired this source | Recover publication, or use the successor |
| Encrypted capture failed with `retired.json` but no `ENCRYPTED-COMPLETE.json` | Publication failed after retirement | `recover-encrypted-publication` |
| `Stop application and worker processes before backup` | A participant still holds its lease | Stop it; never delete lease files |

Never retried automatically: a corrupted or mismatched `retired.json` or a receipt that fails to verify
(keep the material and get review); re-binding a retired source to another capture; undoing a
retirement (removing a marker by hand defeats the single-writer rule); resuming an activated target.

## Command reference

All commands are `python -m app.backup.manage <command>`. They never stop or start services, deploy
code or activate a recovered application unless stated. Every one asks for the
[backup password](#backup-password) without echo, or reads one line from `--backup-password-stdin`.
Database credentials come from `PGPASSFILE`, never a command-line password. Stop the source
application and every external writer for captures. Practise on test libraries, never the live one.

### Recover a lost library, step by step

The order a recovery drill ran in on Windows with a synthetic library. You need the backup folder, the
administrator password it was made with, this version of FamilyFoto, and PostgreSQL tools of the same
major version. `RECOVERY` is a new folder of your choice.

1. **Start an empty database for the recovery** on an unused port. (The drill used `trust`, which lets
   any local program connect; password login hasn't been tried with these steps.)

   ```text
   initdb -D RECOVERY\pg -U recovery_admin -A trust -E UTF8 --locale=C
   pg_ctl start -D RECOVERY\pg -l RECOVERY\pg.log -o "-p RECOVERY_PORT -h 127.0.0.1"
   ```

2. **Check and restore** with `verify-library`, then `restore-library` into `RECOVERY\target`
   ([Restore the combined library](#restore-the-combined-library)).
3. **Prepare what the app needs.** `prepare-recovery-tls` with the names the new computer answers to,
   plus `127.0.0.1`; `prepare-recovery-role` twice into separate new folders, once with `--preview`.
4. **Validate** with `validate-recovery`.
5. **Look at it** with `preview-recovery` and sign in with the old password. Everything is read-only;
   thumbnails are drawn as you scroll and videos show none until step 8. Ctrl+C, or a file named `STOP`
   in the scratch folder, closes it.
6. **Switch to it** with `prepare-activation`, then `commit-activation`. Use `--source-unavailable` only
   when the old library is really gone.
7. **Start it** (keep the window open; nothing runs it as a service yet):
   `python -m app.migration.launch --directory RECOVERY\target`
8. **Put the thumbnails back** while the server runs:
   `python -m app.backup.manage restore-thumbnails --directory LIBRARY_BACKUP --target-directory RECOVERY\target`.
   It never replaces an existing thumbnail and can be repeated (on Windows, files the server has open
   are counted `in_use` and left for the next run). Then rebuild what's still missing with
   `python -m app.migration.launch --directory RECOVERY\target --repair-thumbnails`; each item is checked
   against the catalog first, so a library with no thumbnails takes hours. Posters need FFmpeg: on
   another computer, point `video_tools.ffmpeg` and `video_tools.ffprobe` in
   `RECOVERY\target\catalog\files\config\local.json` at this computer's copies first.
9. **Back it up** from Settings. If `pg_dump` isn't found, set `backup.pg_dump` in that same file.

After a restart, start the database with the `pg_ctl` line and repeat step 7. Archived scans stay paused.

**Face engine on a recovered library.** With the database running and the server stopped:

```text
python -m app.migration.people_setup --directory RECOVERY\target --research-only [--rebuild-thumbnails] [--limit N] [--skip-check]
```

It installs this computer's face engine and makes it the active profile (the first time this needs the
network), reuses the library's `people` catalog if this computer's model is the same space (never making
a second catalog, and refusing a library from before the catalog migration), and runs the library check:
it samples the clearest faces (score 0.95 or more, one per photo; down to 0.85 only if fewer than 12) and
stores the certificate, or prints why it failed and exits non-zero without writing a vector.
`--skip-check` leaves the check to the first Discover people. `--rebuild-thumbnails` makes the face
thumbnails a backup leaves out (it reads every photo, about 1.6 hours for 24,000 photos on Windows, and
only writes `data/people/thumbnails`). Until this is done People is view-only;
`app.migration.launch` decides at each start, from the catalog and certificate, whether People can be
edited.

### Capture

```text
python -m app.backup.manage capture --offline --host 127.0.0.1 --port SOURCE_PORT --username SOURCE_USER --dbname SOURCE_DATABASE --pg-dump PATH_TO_PG_DUMP --config-root SOURCE_CONFIG --data-root SOURCE_DATA --directory NEW_BACKUP_DIRECTORY --database-budget-bytes CONSERVATIVE_DUMP_SIZE_BYTES
python -m app.backup.manage verify --directory BACKUP_DIRECTORY
```

Catalog only, no media. `--offline` confirms external file writers and old ungated versions are stopped;
the account must be able to inspect and lock every catalog table. Existing output directories are refused.
Space is checked before and during writes, assuming no compression. Progress is JSON Lines; credentials
and tool errors are never echoed. `journal.json` is progress only, never proof of completion. `verify`
reads every encrypted component and rejects missing, corrupt, swapped or mixed ones; it isn't a restore
drill.

### Combined catalog and plain media

```text
python -m app.backup.manage capture-library --directory NEW_LIBRARY_BACKUP --host 127.0.0.1 --port SOURCE_PORT --username BACKUP_USER --dbname SOURCE_DATABASE --pg-dump PATH_TO_PG_DUMP --config-root SOURCE_CONFIG --data-root SOURCE_DATA --media-root MEDIA_ROOT --database-budget-bytes CONSERVATIVE_DUMP_SIZE_BYTES --offline [--single-pass] [--resume] [--retire-to SUCCESSOR_ID] [--allow-unprotected-destination]
python -m app.backup.manage verify-library --directory LIBRARY_BACKUP
```

Every non-deleted catalog media file must match its recorded size and SHA-256, retained originals
included; confirmed deletion receipts and the legacy completed-AVI removal convention are recognised.
The encrypted `library/` index binds the catalog and the plaintext inventory; `verify-library` checks the
binding, every media file and the thumbnail copy, and needs no source disk.

Retrying with `--resume` (same roots, password and `--retire-to`) takes a new database dump, rereads and
hashes every source file and rehashes every copied file, and saves only the copying already done. Any
source change refuses the retry and leaves the gate. Unusable work is renamed `previous-catalog-*`,
`previous-library-*`, `previous-inventory-*` or `previous-media-copy-*` and never read again; delete it
once the capture is verified. A disk can be reconnected between attempts at the same path (volume ID and
folder inode unchanged); another path or a filesystem without stable inodes is refused. On macOS exFAT,
empty files get a new inode on remount, which is allowed only as described under [Filesystems](#filesystems).
Role staging `--resume`, preview, activation and disk moves still compare device numbers, so they continue
only while their disks stay mounted.

### Restore for validation (catalog only)

```text
python -m app.backup.manage restore --directory BACKUP_DIRECTORY --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN --pg-restore PATH_TO_PG_RESTORE --expected-cluster-directory RECOVERY_CLUSTER_DATA_DIRECTORY --target-directory NEW_PRIVATE_RECOVERY_DIRECTORY --database-budget-bytes CONSERVATIVE_RESTORED_DB_SIZE_BYTES
```

Use a **separate** recovery cluster on another port, of the same PostgreSQL major version, and only
trusted archives. The cluster's real data directory is checked first. It creates a random new database,
remaps ownership to the recovery account, removes old grants, verifies table inventories and row counts,
clears sessions and login throttles, pauses unfinished uploads and imports, and requests pause for face
jobs. The database stays read-only by default and `PUBLIC` can't connect. Settings get new session
secrets, no database password, a disabled user and no disk binding; face auto-start is off, and normal
startup refuses these settings.

### Restore the combined library

```text
python -m app.backup.manage restore-library --directory LIBRARY_BACKUP --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN --pg-restore PATH_TO_PG_RESTORE --expected-cluster-directory RECOVERY_CLUSTER_DATA_DIRECTORY --target-directory NEW_PRIVATE_RECOVERY_DIRECTORY --database-budget-bytes CONSERVATIVE_RESTORED_DB_SIZE_BYTES [--resume] [--in-place] [--media-directory DIR --allow-unprotected-media]
```

It verifies the backup, checks combined space, restores the catalog into a random new database, copies
media into `media-copy/media/` (compared by content and layout, not inode) and reads it all back.
Config and data go to `catalog/files/`. `LIBRARY-RECOVERY-REPORT.json` records the media location;
everything stays validation-only. If a phase fails, `LIBRARY-RESTORE-INCOMPLETE` stays and the database
identity is kept in `catalog/database-target.json`. Never start a restore marked incomplete. Repeat with
`--resume` to continue it (backup, identities, endpoint and cluster must match); a restore killed before
recording any work says so and needs a new target. Completed targets can't be retried or overwritten.
Capacity checks stay conservative on retry.

#### Restore in place, when the disk can't hold two copies

`--in-place` moves the backup's media into the recovery folder instead of copying it, for a library and
backup that won't both fit (for example 2.7 TB on a 4 TB SSD). The new folder must be on the backup's
disk. **The backup is used up:** it keeps its catalog and a `MEDIA-MOVED.json` record of where the media
went, but can't be restored again. Keep the original library until the moved one has been activated and
backed up again.

1. Refuses a folder on another disk before touching anything.
2. Rereads and hashes every file in the backup.
3. Restores the catalog into a new database.
4. Writes `MEDIA-MOVED.json`, then renames `media-copy` into the recovery folder.
5. Walks the moved files; unchanged names, sizes and times stand for step 2's read and leave a receipt
   ([Reading the media once](#reading-the-media-once)). Anything else is reread.

Only catalog and database space is needed. Repeat with `--resume` after any stop; a resume started
differently, a restore of the used backup elsewhere, or media found in both places is refused. If the
rename itself fails nothing moved and the backup is still whole. **Never delete an unfinished in-place
recovery folder or its media folder:** after the rename it holds the only copy.

**Photos on an exFAT disk.** The recovery folder holds credentials, so it's always refused on a disk
without permissions. Split the restore: `--target-directory` on a protected disk (catalog, database,
settings, credentials) and `--media-directory` as a new folder on the backup's disk, with
`--allow-unprotected-media` accepting that anyone with the disk can read the photos (as they already
could). The library's `media_root` points at the media folder. Resume with the same `--media-directory`
and keep the disk connected through activation. The Settings restore test doesn't offer in-place or split
restores.

```bash
python -m app.backup.manage restore-library --in-place --directory /Volumes/SSD/familyfoto-backup --media-directory /Volumes/SSD/familyfoto-library --allow-unprotected-media --target-directory ~/familyfoto-recovery ...
```

### Prepare a recovered computer

```text
python -m app.backup.manage prepare-recovery-tls --source-tls RESTORED_CONFIG_TLS_DIRECTORY --directory NEW_PRIVATE_TLS_DIRECTORY --name new-host.local --name 192.0.2.10 --name localhost --name 127.0.0.1 --name ::1
python -m app.backup.manage prepare-recovery-role --database-record RECOVERY_DATABASE_TARGET_JSON --expected-cluster-directory RECOVERY_PG_DATA --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN --directory NEW_PRIVATE_CREDENTIAL_DIRECTORY [--preview] [--resume]
```

- **TLS.** The new directory must not exist. The CA bytes are kept exactly; only the server key and
  certificate are new, expiring no later than the CA. The CA must be a valid self-signed authority with
  a matching key and more than 31 days left; anything else is refused, never regenerated.
  `TLS-REPORT.json` holds fingerprints and names, not keys. It installs no trust and changes no settings.
- **Role.** The record must identify a verified, sanitised, read-only recovery database by name, OID,
  ownership token and cluster directory. The role has no login, superuser, creation, replication,
  inheritance, RLS bypass or schema-creation rights, only the table, sequence and function permissions
  the app needs. `credentials.json` holds the secret in a private directory; treat the whole directory
  as secret. `--resume` reuses the exact owned role and refuses anything changed. `--preview` stages a
  role with table reads only; switching modes on an existing journal is refused. The role stays
  `NOLOGIN` until preview or activation enables it; never enable it by hand.

### Validate, preview and activate

```text
python -m app.backup.manage validate-recovery --directory ORIGINAL_BACKUP --target-directory RECOVERY_DIRECTORY --role-directory PRIVATE_CREDENTIAL_DIRECTORY --tls-directory PREPARED_TLS_DIRECTORY --expected-cluster-directory RECOVERY_PG_DATA --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN
python -m app.backup.manage preview-recovery --directory ORIGINAL_BACKUP --target-directory RECOVERY_DIRECTORY --role-directory PRIVATE_PREVIEW_CREDENTIAL_DIRECTORY --tls-directory PREPARED_TLS_DIRECTORY --expected-cluster-directory RECOVERY_PG_DATA --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN --scratch-directory NEW_PRIVATE_SCRATCH_DIRECTORY --listen-port 4439
python -m app.backup.manage close-recovery-preview --database-record RECOVERY_DATABASE_TARGET_JSON --directory PRIVATE_PREVIEW_CREDENTIAL_DIRECTORY --expected-cluster-directory RECOVERY_PG_DATA --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN
python -m app.backup.manage prepare-activation --directory ORIGINAL_BACKUP --target-directory RECOVERY_DIRECTORY --role-directory PRIVATE_WRITE_CREDENTIAL_DIRECTORY --tls-directory PREPARED_TLS_DIRECTORY --expected-cluster-directory RECOVERY_PG_DATA --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN --intent-directory NEW_SEALED_INTENT_DIRECTORY --hostname recovered.local --listen-port 4340 --accepted-check library --accepted-check albums-collections --accepted-check people --accepted-check recently-deleted --accepted-check users-permissions --accepted-check sample-playback [--source-unavailable]
python -m app.backup.manage commit-activation --intent-directory SEALED_INTENT_DIRECTORY --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN --offline [--resume [--rollback]]
python -m app.migration.launch --directory RECOVERY_DIRECTORY
```

- `validate-recovery` checks media, the sanitised database against its archive, role privileges, and the
  CA against the backed-up CA, and compares restored file state to the archive, accepting only the
  session reset and worker pauses. It's a point-in-time check, not an activation authorisation. For an
  encrypted restore pass `RECOVERY_DIRECTORY/metadata-job/payload` as `--directory`.
- `preview-recovery` needs a `--preview` role and `127.0.0.1` on the certificate, a new scratch folder and
  an unused HTTPS port. Open `https://127.0.0.1:4439` and sign in normally (trust the backed-up CA through
  the OS if needed). Keep the source offline. It closes on Ctrl+C or after 55 minutes, then revokes
  database access; a taken port fails rather than killing its owner. People are viewable without an
  inference provider: missing face thumbnails are rendered in memory from the photo after checking
  visibility and checksum, and nothing is written.
- `close-recovery-preview` revokes a preview login left by a killed process (disables login, then
  terminates only that role's connections). Safe to repeat.
- `prepare-activation` revalidates, then seals an intent (encrypted with the backup's key) using a
  separate write-capable role. The accepted checks are your explicit acceptance after inspecting the
  read-only copy. The host must be on the certificate, target storage must be available, and an existing
  enrolment marker blocks it. A migration needs its retirement receipt; otherwise `--source-unavailable`
  declares the old source gone.
- `commit-activation` verifies again, publishes an encrypted write-access fence **before** enabling
  login, journals the switch and enables the role. After an interruption repeat with `--resume`;
  `--resume --rollback` is allowed only before the fence. Intents from earlier prototypes must be
  prepared again.
- `app.migration.launch` runs the activated library with its own configuration, data, logs and temp.
  Archived scans stay paused.

`preview-recovery` and `prepare-activation` refuse what they can before any long read: missing preview
packages, a hostname not on the certificate (exact match, including case), a preview credential, and a
source that is neither retired nor declared unavailable.

#### Reading the media once

Reading every file takes hours, so it happens once and leaves a sealed receipt: `MEDIA-VERIFIED.json` in
the recovery folder (or `BACKUP-MEDIA-VERIFIED.json` for a backup that still holds its media), naming the
disk, folder, inventory, and a digest of every file's name, size and modification time.
`validate-recovery`, `preview-recovery`, `prepare-activation` and `commit-activation` then only walk the
files and accept the receipt when nothing differs; any difference, or a missing or foreign receipt, means
a full read again. A receipt can't see bytes changed without a size or time change, so add
`--reverify-media` to read everything again, for example before `commit-activation` when days have
passed. Encrypted candidates have no receipt and read their media every time.

### Disk moves

```text
python -m app.backup.manage prepare-disk-move --offline --host 127.0.0.1 --port SOURCE_PORT --username SOURCE_USER --dbname SOURCE_DATABASE --pg-dump PATH_TO_PG_DUMP --config-root SOURCE_CONFIG --data-root SOURCE_DATA --directory NEW_DISK_JOB_DIRECTORY --backup-directory NEW_CATALOG_BACKUP_DIRECTORY --database-budget-bytes CONSERVATIVE_DUMP_SIZE_BYTES [--resume]
python -m app.backup.manage plan-disk-cutover --directory PREPARED_DISK_MOVE
python -m app.backup.manage commit-disk-move --offline --accept-offline-rehash --host 127.0.0.1 --port SOURCE_PORT --username SOURCE_USER --dbname SOURCE_DATABASE --pg-dump PATH_TO_PG_DUMP --config-root SOURCE_CONFIG --data-root SOURCE_DATA --directory PREPARED_DISK_JOB_DIRECTORY --backup-directory NEW_CUTOVER_CATALOG_BACKUP --database-budget-bytes CONSERVATIVE_DUMP_SIZE_BYTES
python -m app.backup.manage recover-disk-move --offline --host 127.0.0.1 --port SOURCE_PORT --username SOURCE_USER --dbname SOURCE_DATABASE --config-root SOURCE_CONFIG --data-root SOURCE_DATA --directory PREPARED_DISK_JOB_DIRECTORY [--rollback]
```

- **Prepare** copies the enrolled media to `NEW_DISK_JOB_DIRECTORY/copy/media` and makes a separate
  catalog backup; job, backup, application state and original media must all be independent. The source
  endpoint must match the settings and the source media must have a matching marker. It publishes
  `READY.json` only on success. **Ready is not activation**: nothing in the source changes, and never point
  the app at the prepared copy by hand. `--resume` reuses an owned copy while the original gate is still
  held.
- **Plan** estimates downtime from full source and target reads at stated throughputs (not measured
  speed, excluding catalog backup time).
- **Commit** takes a new catalog backup, rechecks both copies, and switches only `destination`. An
  encrypted `cutover-intent` records the old and new configuration before the marker or settings change.
- **Recover** finishes an interrupted switch, or with `--rollback` restores the old settings, only while
  the original gate is still closed. Once released, rollback is refused because the new library may have
  been written. Neither copy is ever deleted.

### Plain media inventory and copy

```text
python -m app.backup.manage media-inventory --source-root MEDIA_ROOT --data-root SOURCE_DATA --directory NEW_INVENTORY_DIRECTORY --offline
python -m app.backup.manage media-copy --source-root MEDIA_ROOT --data-root SOURCE_DATA --inventory INVENTORY_DIRECTORY --directory NEW_COPY_JOB_DIRECTORY --offline
python -m app.backup.manage media-verify --directory COPY_JOB_DIRECTORY
```

Media-only operations on explicitly named offline roots, under the DATA maintenance gate. They are not a
paired recovery point, and must never be used for configuration, credentials or face state. The copy job
holds `media/` in the original layout, a private copy of the inventory and per-file journals. SHA-256
detects accidental damage but doesn't authenticate against someone able to alter files and manifests.
Retry the same `media-copy` to reuse verified prefixes. `TREE-INCOMPLETE` always overrides an older
completion report, and only full verification publishes `TREE-COMPLETE.json`.

### Encrypted whole-library repository

```text
python -m app.backup.manage restic-init --restic RESTIC_EXECUTABLE --restic-sha256 EXECUTABLE_SHA256 --repository NEW_REPOSITORY --repository-password-file PRIVATE_REPOSITORY_PASSWORD_FILE
python -m app.backup.manage capture-encrypted --offline --host 127.0.0.1 --port SOURCE_PORT --username SOURCE_USER --dbname SOURCE_DATABASE --pg-dump PATH_TO_PG_DUMP --config-root SOURCE_CONFIG --data-root SOURCE_DATA --media-root SOURCE_MEDIA --directory NEW_WORKING_DIRECTORY --database-budget-bytes CONSERVATIVE_DUMP_SIZE_BYTES --restic RESTIC_EXECUTABLE --restic-sha256 EXECUTABLE_SHA256 --repository REPOSITORY --repository-password-file PRIVATE_REPOSITORY_PASSWORD_FILE --repository-id FULL_REPOSITORY_ID [--retire-to SUCCESSOR_ID]
python -m app.backup.manage restore-encrypted --host 127.0.0.1 --port RECOVERY_PORT --username RECOVERY_ADMIN --pg-restore PATH_TO_PG_RESTORE --expected-cluster-directory RECOVERY_CLUSTER_DATA_DIRECTORY --target-directory NEW_PRIVATE_RECOVERY_DIRECTORY --database-budget-bytes CONSERVATIVE_RESTORED_DB_SIZE_BYTES --metadata-budget-bytes CONSERVATIVE_METADATA_SIZE_BYTES --restic RESTIC_EXECUTABLE --restic-sha256 EXECUTABLE_SHA256 --repository REPOSITORY --repository-password-file PRIVATE_REPOSITORY_PASSWORD_FILE --repository-id FULL_REPOSITORY_ID --metadata-snapshot FULL_METADATA_SNAPSHOT_ID [--resume]
python -m app.backup.manage recover-encrypted-publication --restic PATH_TO_RESTIC --restic-sha256 RESTIC_SHA256 --repository REPOSITORY --repository-password-file PASSWORD_FILE --repository-id REPOSITORY_ID --data-root RETIRED_SOURCE_DATA --directory WORKING_DIRECTORY
```

Keep the repository password file outside the captured roots, working directory and repository. Capture
needs a new working directory and stages no second media copy; all IDs are full IDs, never prefixes. Only
`ENCRYPTED-COMPLETE.json` plus terminal success is a completed capture; keep its metadata snapshot ID.

- `restore-encrypted` read-checks the repository and verifies media hashes, paths, empty directories and
  catalog binding before creating the database. Source drive letters and mount paths are never reused.
  The report's `media_root` points at the verified media-job payload. `--resume` continues an owned
  incomplete job; a database stage already started is reused only after its journal and ownership verify.
- A `capture-encrypted --retire-to` capture retires the source before publishing its metadata snapshot.
  If that fails, keep the working directory and run `recover-encrypted-publication` once the repository
  is available: it authenticates the retained metadata, matches its identities against the retirement
  record, republishes the receipt and a new metadata snapshot, reads the repository back and writes
  `ENCRYPTED-COMPLETE.json`. Use only the snapshot ID in the receipt. A repeat after success publishes
  nothing. It never un-retires the source.

### Failures and maintenance

```text
python -m app.backup.manage acknowledge-failed-maintenance --data-root SOURCE_DATA --maintenance-id EXACT_MARKER_ID
python -m app.backup.manage recover-retirement --data-root RETIRED_SOURCE_DATA --directory LIBRARY_BACKUP_DIRECTORY
```

- A capture failure never replaces an older backup. A maintenance marker survives a crash and blocks
  startup: inspect `.recovery-admission/maintenance.json` and the backup journal, confirm nothing is
  still running, then acknowledge that exact marker ID. OS locking refuses acknowledgement while
  maintenance runs. Acknowledging never resumes scans, deletes artifacts or certifies a backup.
- `recover-retirement` finishes a plain migration receipt that failed after the source was retired. It
  verifies the whole backup and its identities against the retired source, keeps incomplete receipt
  ciphertext as `retirement-incomplete-<id>`, and is idempotent. The source stays retired.
- A restore failure keeps a private incomplete directory and any recovery database; never activate a
  directory containing `RECOVERY-INCOMPLETE`. Nothing is removed automatically.
- Browser upload staging is part of the sealed state. Captures check staged files against the frozen
  upload records, and restored upload and import sessions stay paused.

## Filesystems

| Disk | Windows | macOS | Notes |
| --- | --- | --- | --- |
| NTFS | Supported | Not qualified | macOS reads but doesn't write it by default |
| APFS | Not available | Supported | Windows can't read it |
| exFAT | Supported with `--allow-unprotected-destination` | Measured with the flag | No permissions; the only format both can write |
| FAT32 | Not for large video | Not for large video | |
| Network volumes, anything else | Not qualified | Not qualified | Measure with `scripts/probe_backup_filesystem.py` first |

- **exFAT.** No permissions, so keys, credentials and restored application state are refused there with
  no override; plain captures and restic repositories need the flag. Timestamps round to 2 seconds. No
  journal, so eject properly. A small synthetic library moved between Windows and a Mac in both
  directions on a USB SSD, with accented and CJK names.
- **exFAT on macOS** specifically: `chmod` succeeds but everything reads back `0o700`, so a disk is
  refused unless two different modes read back. There are no hard links and a plain rename silently
  overwrites, so files are published by exclusively creating their name and replacing that empty claim
  (don't let other programs write into a job's folders). macOS writes AppleDouble `._name` sidecars;
  only files with the real AppleDouble or `.DS_Store` format and, in a media tree, the file they describe
  count as sidecars, and an inventoried file is always media. Empty files get a new inode on remount.
  Names are stored composed (NFC) but listed decomposed, so paths are matched by Unicode form (never by
  case) when not found exactly, with one inventory entry answering one name. Keep source libraries on
  APFS or NTFS, not exFAT.
- Windows reserved device names are refused. Every inventoried path is checked against the destination's
  limits before long work (Windows long-path capability, or 259/247 characters without it; `pathconf` on
  macOS; 255 UTF-16 units per name), stopping with a count rather than renaming anything.
- Don't browse job directories or recovered config in Finder or Explorer while a workflow runs:
  `.DS_Store` and `._*` files trip the ownership checks. Never delete unknown entries to get past a check.
- On macOS tests, resolve temporary paths first: `/var` links to `/private/var`, and linked directories
  are refused by design.

## Security

- Database archives hold password hashes and face data. They, the state archive and the activation
  intent are encrypted with a key derived from the administrator password. Plain media is not encrypted.
- Tool output is reduced to allowlisted diagnostic categories; raw stderr isn't stored, and paths,
  credentials and database errors are redacted in reports.
- Every restore needs a new, empty, private target. Linked, hard-linked, special or unreadable output is
  rejected.
- Preview and the restore test are not a sandbox against malicious archived database code.
- Restore accepts an explicitly chosen older recovery point; its report includes the authenticated
  `captured_at`. Check it before activating: valid isn't the same as recent.

## Not built yet

- Online or shortened-gate capture, per-version pins and filesystem snapshots.
- Retention, pruning, scheduling and rotating verification; history is manual.
- Whole-capture retry for the encrypted format; continuing on a disk reconnected at another path; role
  staging, preview, activation and disk moves that still compare device numbers.
- Network and offsite repositories, restic provisioning on macOS, installer-managed tool provisioning.
- In-place and split restores from the Settings page.
- Cross-platform and TB-scale acceptance beyond small synthetic libraries: physical disconnect and power
  loss, reboot, activated-browser drills, native People inference on a new computer, and measured KDF
  timing on supported devices.

## Tests

Backup tests use synthetic data and a disposable PostgreSQL cluster in this checkout's
`temp/backup-plan-pg`, selected with `FAMILYFOTO_BACKUP_TEST_PORT` (user `backup_test`; on macOS also
`FAMILYFOTO_BACKUP_PG_BIN`). The fixture checks `SHOW data_directory` and creates and drops only random
databases; never point it at a running library. Real-restic tests need `FAMILYFOTO_BACKUP_RESTIC` and
`FAMILYFOTO_BACKUP_RESTIC_SHA256`:

```text
python -m pytest tests/test_backup_restic.py tests/test_backup_encrypted_library.py -q
python -m pytest app/fastbackup/tests -q
```

Fault injection covers interrupted copies, journals, publications and database creation, wrong
passwords, damaged chunks, changed disks, full disks, mutated inputs and a second writer. An archive
listing is not a restore test: exercise a restore regularly on a disposable installation.

References (what each tool can do, not what FamilyFoto guarantees):
[pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html),
[restic scripting](https://restic.readthedocs.io/en/stable/075_scripting.html),
[Fernet](https://cryptography.io/en/44.0.3/fernet/),
[cryptography Scrypt](https://cryptography.io/en/44.0.3/hazmat/primitives/key-derivation-functions/#scrypt).
