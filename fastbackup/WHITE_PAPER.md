# FamilyFoto Fast Backup

## Abstract

Fast Backup is FamilyFoto's offline, full-verification backup engine for large
photo and video libraries on Windows and macOS. It separates media transfer from
verification, uses concurrent native file copying on Windows, and reuses the
application's catalog, encryption and recovery infrastructure. Its output is a
complete recovery point, not merely a copied media folder.

The central trade-off is deliberate: Fast makes each attempt a new full copy,
without resumable media checkpoints. It reduces work in the transfer path while
retaining full destination verification before success. Standard remains the
resumable alternative. Both verify every media file; Fast has no partial or
percentage-based verification mode.

This paper describes the code in `develop` at `3367285` and measurements from the
Windows implementation at `fbb437d`. It is a description of the current system,
not a development history or a claim of guaranteed throughput.

## 1. Goals and scope

Fast Backup is designed to:

- Reduce media-copy overhead for libraries containing many files and potentially
  terabytes of content, especially when copying to removable storage.
- Preserve a consistent combination of database, application state and media.
- Make copying, verification and finalization separately observable.
- Produce the existing full-library recovery format, usable for recovery and
  migration to another disk or computer.
- Keep originals unchanged and withhold success when any required stage fails.

It is not a disk image, filesystem mirror, incremental backup, synchronization
service or continuous online snapshot. It does not deduplicate media or resume a
failed Fast attempt. It does not copy the application's installed Python runtime
or running PostgreSQL data directory as a system image. Recovery uses application
software and database tools supplied by the destination installation.

### Choosing an engine

| Property | Standard | Fast |
| --- | --- | --- |
| Main implementation | `app/backup/` | `fastbackup/` |
| Intended trade-off | Resume interrupted capture | Simpler high-throughput transfer |
| Interrupted attempt | Retry using Standard's resume rules | Start a new full copy in a new folder |
| Media assurance | Full verification | Full verification |
| Application availability | Offline during capture | Offline during capture |
| Default selection | Explicit selection | Yes |

Fast is available on every installation and is the default for new backups; its
internal strategy identifier is `fast-full`. Standard remains selectable. Both
methods require full verification. Existing runs retain their recorded method;
older runs without a method continue to use Standard when retried.

## 2. Consistency and operating assumptions

The application-controlled workflow confirms the operation and performs preflight
before stopping FamilyFoto. During capture, the maintenance gate excludes
participating application writers, and database table/advisory locking provides
the coordinated catalog consistency window.

The source must remain immutable and the destination must be exclusive to the
backup. Operators must stop external programs that could edit either tree.
Stopping FamilyFoto does not stop unrelated file managers, synchronization tools
or scripts. Fast is not a filesystem snapshot and does not attempt to compensate
for arbitrary concurrent external writers.

Under this contract, the media inventory does not repeat a source-tree scan to
detect changes after enumeration. Windows transfer also avoids per-file source
stat calls, stream enumeration and parent-handle pinning. Top-level root checks,
inventory validation, destination verification and publication gates still apply.
Destination files are not kept locked across the copy and verification phases.

## 3. Architecture

```text
Settings: select Standard or Fast, confirm Start backup
                         |
             preflight and capacity checks
                         |
       detached runner stops the application
                         |
      maintenance gate + database consistency window
                         |
 inventory -> catalog/state -> thumbnails -> media worker
                                              |
                         prepare -> copy all -> verify all
                                              |
                               final tree checks + clean exit
                                              |
                     authenticate/publish recovery index
                                              |
                              request application restart
```

The detached runner owns the operation, not the browser. The modal displays
progress and expandable logs; closing it or restarting the browser does not itself
cancel a running backup. This independence does not protect against shutdown of
the backup computer or termination of its worker processes.

### Implementation map

| Component | Responsibility |
| --- | --- |
| [Settings](../web/backup-settings.mjs) and [progress](../web/backup-progress.mjs) | Method descriptions, confirmation, progress, logs and elapsed-time history |
| [Application API](../app/backup/admin_api.py) and [runner](../app/backup/admin_runner.py) | Preflight, detached execution, server stop/restart and retry decisions |
| [Capture coordinator](capture.py) | Consistency window, catalog/state/media ordering and final authenticated index |
| [Media worker](copy_worker.py) | Process isolation, progress transport, deadline and clean-exit confirmation |
| [Full copy](full_copy.py) | Inventory admission, capacity, transfer/verification phases and media completion |
| [Windows bulk copy](windows_bulk_copy.py) | Bounded concurrent CopyFile2 calls |
| [Portable copy](native_copy.py) | macOS/POSIX copying and platform flush helpers |
| [Library coordinator](../app/backup/library.py) | Shared capture coordination, reconciliation and full-format validation |
| [Catalog](../app/backup/catalog.py), [encrypted artifacts](../app/backup/encrypted.py) and [media inventory](../app/backup/media_inventory.py) | Database/state capture, authenticated encryption and inventory construction |
| [Restore facade](restore.py) | Delegation to the existing inactive-target recovery path |
| [Cleanup](cleanup.py) | Explicit, ownership-checked reclamation of incomplete Fast attempts |

The top-level `fastbackup/` package is an alternate engine, not an independent
application or duplicate recovery system. Shared components remain in
`app/backup/`.

## 4. Capture pipeline

### Preflight and admission

Start backup first presents confirmation, then checks the plan. Preflight checks
destination suitability, available capacity and application state before taking
the application offline. Capture performs further capacity checks as exact
inventory sizes become available. The destination must be separate from the
media, data and configuration source roots and must be a new attempt directory.

Space admission includes media, catalog/application data and working overhead,
not just the photo byte total. During media transfer, remaining capacity is
rechecked after approximately 1 GiB copied or 30 seconds, at transfer progress
boundaries. A single active OS copy is not interrupted by that timer. Disk-full
write errors still fail the attempt. An upfront check cannot reserve space
against unrelated processes filling the disk later.

### Read the library

A SQLite inventory records relative paths, entry types, sizes and SHA-256
digests. Portable-name checks detect conflicts such as names that would collide
on another supported filesystem. Empty directories are represented too.

The engine reuses eligible database checksums when the catalog path and size
match; otherwise it reads and hashes the source file. This avoids rereading every
source byte solely to establish a checksum, but relies on the immutable-source
and catalog-integrity assumptions. A matching path and size is not a fresh proof
that an externally edited source still matches an old checksum.

Catalog-to-inventory reconciliation checks that the captured media and database
agree. Reading the library therefore does more than listing filenames and can be
significant for many small files or files without reusable hashes.

### Save catalog and application data

The database is captured through the existing PostgreSQL dump machinery.
Application data is a policy-selected set of authoritative configuration and
file-side state, not an indiscriminate copy of the installation directory.
Examples include local settings, required identity/People state and certificate
material. Logs, transient process controls and running database files are not
replayed as authoritative application state. Unknown state blocks capture until
its recovery treatment is defined.

Application data is archived and encrypted using the shared state-capture
implementation. The catalog coordinator then rereads and authenticates both the
sealed database dump and state archive before moving on to thumbnails. Its own
consistency checks remain; the immutable-media
optimization is not a blanket removal of all file checks from the backup system.
Thumbnails and previews are captured separately as recoverable caches.

Catalog, application state and thumbnail capture occur **before** media transfer.
The current engine is not a media-first pipeline.

### Copy all media

Preparation checks the saved inventory's source binding and completeness,
snapshots the inventory for the worker, checks roots and admits capacity. It does
not enumerate the whole source tree again.

On Windows, directory creation and file copying both belong to the
`media-transfer` phase. The worker validates inventory names/parents in memory,
creates destination directories, then uses CopyFile2 with **16 threads** and at
most **32 queued/in-flight futures**. Copy flags prohibit overwrite and skip
alternate data streams. Unexpected links are not followed as ordinary media;
destination verification prevents their publication as a valid recovery point.

The fast Windows loop copies file contents without hashing each file immediately
after copying it. Completion callbacks update counts, capacity and progress.
This is file-level concurrency, not a block-level disk clone.

On macOS/POSIX, files are copied sequentially through exclusive, no-follow file
descriptors with bounded 8 MiB buffers and per-file `fsync`. This is a portable
implementation, not a claim that a native macOS engine or optimal concurrency
level has been established. Media copying does not preserve arbitrary ACLs,
extended attributes or resource forks as a system image would.

### Verify all media and publish

After copying finishes, every destination file is read and checked against its
inventory SHA-256. Windows uses short-lived handles, flushes each file and checks
its size/type/identity around verification. macOS uses the full tree verifier.
In addition to per-file `fsync`, Darwin receives one `F_FULLFSYNC` request through
the inventory snapshot before media verification and another through
`TREE-COMPLETE.json` after that completion record is written.

A final consistency pass checks the destination tree without a second content
hash pass, plus inventory binding and top-level volume identities. Missing,
unexpected or inconsistent entries prevent completion. Flush/read-back uses the
normal OS filesystem path and may read cached data; it is not an uncached
physical-media certification or a guarantee against faulty storage hardware.

The worker must report a successful result **and exit cleanly**. The parent
does not treat the last copied file or a result message alone as completion.
The capture coordinator then publishes the catalog and authenticated library
index; the runner checks the finished metadata/thumbnail artifacts and records
verification before requesting restart.

## 5. Recovery point, security and failure handling

The recovery point uses `familyfoto-library-plain-media-v2`. Its authenticated
index binds the capture/library identity, catalog, media inventory digest and
media totals. Principal payloads are the catalog/state, inventory, plaintext
media tree and thumbnail artifact, with password context, completion records and
restore handover files.

The administrator password used at capture protects the catalog and application
secrets through the existing encrypted-artifact machinery. That password is
needed for recovery even if the live account password later changes. **Media
files and filenames are plaintext.** An exFAT destination requires explicit
acceptance that filesystem permissions cannot protect those files.

Restore reuses the full-format verifier and recovery implementation. The
standalone Fast restore interface copies into a separate inactive target; it
does not consume the backup, activate the recovered installation or start its
services. Migration and activation remain explicit operations. A successful
capture is evidence of a verified backup, but a restore drill is still needed to
demonstrate recovery on the intended target machine.

On disconnect, copy/verification error, timeout or interruption, incomplete
artifacts are retained rather than promoted to a recovery point. Application
maintenance remains failed/stopped until the supported recovery decision is
made. Fast retry requires capacity for a new complete attempt; it does not reuse
copied files from the failed attempt.

The worker has a configurable overall deadline (seven days by default) and a
bounded clean-exit wait. Windows cancels queued copies and joins active CopyFile2
calls on ordinary failure/cancellation; an individual OS copy is not interrupted
by a per-file callback. The parent handles worker timeout/termination and does
not accept success without confirmed exit.

Incomplete-attempt cleanup is explicit: preview authenticated ownership and the
candidate tree, then confirm the exact fingerprint. It rejects completed
captures, active work, source overlap and unexpected entries. It is not automatic
garbage collection and does not authorize removal of arbitrary folders.

## 6. Measured performance

### Dataset and measurement boundaries

The available Windows comparison uses **36,129 media files**, **11,757
directories** and **8,486,073,335 bytes**: 8.49 GB decimal, or 7.90 GiB. Source
media was the test-server library on D:, not the live WD HDD library. The target
was the authorized 2 TB SanDisk SSD on F:, formatted exFAT, using new destination
folders. The source drive model, negotiated USB speed and cache state were not
controlled in the recorded experiment.

| Measurement | Elapsed | Effective payload rate | What it measures |
| --- | --- | --- | --- |
| Copy-only program | 215.289 s (3m35s) | 39.4 MB/s | Directory creation and 16-thread CopyFile2; no content verification or explicit durability flush |
| Integrated media-transfer phase | 600.453 s (10m00s) | 14.1 MB/s | Directory creation, bounded copy scheduling, progress/capacity integration and copies |
| Integrated media verification | 716.797 s (11m57s) | 11.8 MB/s | Full destination flush/read/hash verification |
| Integrated final media checks | 264.328 s (4m24s) | Not a transfer rate | Tree, inventory and volume consistency |
| Entire application-controlled backup | 3,904.311 s (1h05m04s) | Not a copy-engine rate | Start through finish, including waits and restart |

Rates use decimal MB and payload bytes only; they are not raw device bandwidth.
The copy-only output was explicitly **copied-not-verified**, not a recovery point.
The integrated run finished with `verification=full`, `verified_at` set and no
reported error.

### End-to-end breakdown

These intervals come from the integrated run's local logs on 2026-11-21
(Pacific time). Except for the explicitly instrumented media phases above,
durations are wall-clock intervals between adjacent log boundaries, not isolated
CPU or I/O measurements. They include intervening work and logging granularity.

| Stage | Approximate time | Work included |
| --- | --- | --- |
| Start, stop application, initialize capture | 2m28s | Runner startup, stop wait and initial state work |
| Read library | 11m20s | Inventory, required source hashes and reconciliation |
| Save database catalog | 1m50s | Database capture before application-state progress |
| Save application data | 13m50s | Application-state capture/encryption plus rereading and authenticating the sealed database dump and state archive |
| Save thumbnails | 6m46s | Cache capture |
| Check saved thumbnails | 16s | Thumbnail artifact checking |
| Worker startup and media preparation | About 2s | Handoff and root/inventory preparation; measured preparation itself was 0.344s |
| Copy media | 10m00s | Directory creation and file transfer |
| Verify media | 11m57s | Full read-back verification |
| Final media checks | 4m24s | Tree and inventory consistency |
| Finalize backup | 1m37s | Clean worker handoff, reconciliation/publication and runner checks |
| Request application restart and finish | 34s | Restart request and completion bookkeeping |
| **Total** | **1h05m04s** | Rounded stage durations sum to this total |

Preflight is outside that run's recorded start-to-finish boundary. Verification
is outside the 10-minute transfer interval: the copy phase is not secretly doing
the subsequent full hash pass.

The application-data interval ends at the thumbnails boundary, so its 13m50s
must not be attributed to state capture alone. The logs do not separately time
state capture and the following sealed-component authentication reads.

### What the results establish

The integrated transfer took about **2.79 times** the copy-only duration. Both
use CopyFile2 and 16 workers, but their surrounding work differs: the integrated
path validates inventory relationships, bounds submitted work, reports progress
through a worker/parent channel and tracks capacity. Directory setup is also
inside both measurements. Cache and storage state can differ between runs.
The measurements do not isolate how much of the gap belongs to each factor;
assigning all of it to checks, logging or the operating system would be speculation.

The end-to-end result also shows that transfer accounts for only about **15%** of
this backup's elapsed time. Reducing it to the copy-only duration, with everything
else unchanged, would save about 6m25s, not turn this into a 3m35s backup.
Inventory, application-data capture, verification and final tree checks are
material components of total downtime.

This small-file-heavy D:-to-SSD test does not establish the runtime for a 3 TB
WD HDD-to-SSD library, a macOS machine, or a different USB/storage layout. It is
not a controlled Standard-versus-Fast comparison. No 7-hour or other TB-scale
completion guarantee follows from it.

## 7. Validation and remaining qualification

Correctness coverage includes full capture/restore compatibility, content
corruption, missing/extra entries, worker termination, cancellation, capacity,
password binding, retained incomplete attempts, migration consumers and UI
behavior. Recorded review evidence includes:

- Windows backup/Fast suite: 1,058 passed, 132 platform/opt-in skips; separate
  migration/move-in/full-capture suite: 107 passed, 79 skips.
- macOS Fast suite: 118 passed, 15 skips; broader backup/recovery/migration suites
  with disposable PostgreSQL 17: 1,259 passed, 57 skips.
- JavaScript unit suite: 211 passed; desktop/mobile browser fixtures passed.

These suites overlap and their counts must not be added as unique tests. They
establish tested behavior, not physical throughput or power-loss guarantees.
Representative TB-scale runs, repeated cold/warm-cache comparisons, disk-loss
drills and cross-machine recovery remain distinct qualification activities.

A useful performance protocol records code revision, file/directory counts,
byte total, source/destination hardware, filesystem, USB connection, cache
conditions and background load. It measures directory creation, transfer,
verification, catalog/state work and total downtime separately, then performs an
isolated restore. Throughput tuning should be judged on repeatable complete
recovery points, not copy-only speed alone.

## Evidence and source references

- Current behaviour, commands and cleanup: [Backup and restore](../docs/BACKUP.md#standard-and-fast).
- Shared [state policy](../app/backup/policy.py),
  [media inventory](../app/backup/media_inventory.py),
  [state capture](../app/backup/state_files.py) and
  [library restore](../app/backup/library_restore.py).

This document does not authorize a deployment, benchmark against live data or
deletion of any existing backup.
