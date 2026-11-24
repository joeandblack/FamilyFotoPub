# Import and upload

How media enters the library, how each import is processed, and who can see it. Source
files are never modified, renamed or deleted.

Contents: [Two ways in](#two-ways-in) · [Sources and settings](#sources-and-settings) ·
[Duplicates and ownership](#duplicates-and-ownership) · [Per-import processing](#per-import-processing) ·
[Imports page](#imports-page) · [Browser uploads](#browser-uploads) · [Visibility](#visibility) ·
[Ignored and unsupported files](#ignored-and-unsupported-files) · [Audio](#audio) ·
[Post-import validation](#post-import-validation) · [Portability](#portability) · [API](#api)

## Two ways in

| | Import | Upload |
|---|---|---|
| What | Copy a folder that the server can read | Send a folder from any computer on the local network |
| Who | Administrators | Administrators and regular accounts; never read-only (`guest`) accounts |
| Where | **Imports** page | **Uploads** page |
| Limits | None (disk copy) | Administrator-set: files per upload, largest file, batch size, staging space, free space kept, piece size, expiry, speed |

A verified upload becomes an ordinary job with `source_kind='upload'`, so metadata,
conversion and duplicate review run unchanged. Imports and uploads are never merged into one
job. All imports and uploads use the same missing-files policy; there is no mode selector
(older clients may still send a mode value, and it is ignored).

## Sources and settings

**Settings** holds the library destination, read-only disk status and the **Ignored file
types** list. Source selection is only on **Imports**. Only administrators can choose or
start server-folder imports or change these settings.

- **Server folder.** Any existing absolute folder on a local drive or a network share the
  server's account can read. Local source volumes are enrolled per import by disk identity
  (see [Operations](OPERATIONS.md#removable-disks)); a share is checked by path and file
  checksums only. No share credentials, drive mappings or permissions are changed. Directory
  links, device paths and overlapping source/destination trees are refused.
- **Destination.** Must be on a locally identifiable disk, in a new or empty folder, and separate
  from the application folder. A library keeps its marker, `.familyfoto-volume.json`, in the
  folder *above* its media folder, so a destination is refused when that parent folder already
  has one. The message names the marker and its library, and says when that library's folder no
  longer exists (a leftover, which you may delete). A folder inside a new empty folder, such as
  `D:\Photos\media`, avoids it. Once any import, catalog
  record or active upload exists, the destination is locked; moving the library needs a
  verified migration, not a path change.
- Each import keeps its source path and volume identity. Resume requires that original source,
  and changing the default never affects existing imports. Saving settings never starts an
  import, needs imports and video work idle or paused, writes atomically, keeps credentials,
  and rejects stale browser settings.
- Imports first selects the latest non-cancelled import's source (even offline), falling back
  to the configured source. Typed or browsed unsaved selections survive refreshes.

## Duplicates and ownership

- Whole-file SHA-256 identifies exact duplicates across the whole catalog, not just the
  selected folder. Metadata-only differences produce a different hash; visual duplicates are
  handled separately by the [photo duplicate scan](LIBRARY.md#photo-duplicates).
- The first successful insertion **owns** the content and its video processing. A later
  exact duplicate records a completed audit row (size, source path, recovery evidence) with no
  media link, so counts reconcile but the item does not appear in a second import or album and
  no work is scheduled again. Only a newly imported file establishes import membership.
- A missing ordinary destination copy may be recovered from a later source with checksum
  verification and a recovery audit. Corrupt destinations and missing converted copies need
  review. Trashed or deleted identities stay deleted: a byte-identical file is counted as a
  duplicate and nothing is recreated or restored.
- Different AVIs can encode to identical MP4 bytes. The first published MP4 stays canonical;
  the later one is linked to it and moved to recoverable Trash atomically, keeping its own
  original path, hash and owner. Restoring it restores the AVI and does not requeue conversion.
- Inventory and completion are durable. A pending duplicate update is atomic, and resume skips
  completed rows. Older aggregate skipped counts stay in progress and history.
- Migration 24 removed secondary duplicate media links (each verified against a retained
  original import record) and dropped the old job mode, preserving counts, ownership, albums
  and People results.

## Per-import processing

Each import has these stages, each with Start / Resume and Pause:

1. **Photo / video import** inventories, copies, verifies and catalogs files.
2. **Video processing** verifies checksums, reads metadata and makes posters ([Video](VIDEO.md)).
3. **Video conversion** converts eligible AVI/FLV/MTS originals ([Video](VIDEO.md#conversion)).
4. **Photo duplicate scan** compares photos and optionally moves clear duplicates to Trash
   ([Library](LIBRARY.md#photo-duplicates)).
5. **Discover people** detects and groups faces ([People](PEOPLE.md)).

- The first four are serialised by one pipeline reservation and the exclusive media-operation
  lock. Discover people is background work that yields between photos. A requested
  server-folder import can ask a duplicate scan to yield at a file boundary.
- Pause stops at a file boundary and cancels automatic continuation. Faults stay stopped for
  review; viewing a page never restarts them. Resume keeps completed work.
- Automatic-stage checkboxes are saved per import and may change while it runs. New imports
  select video processing by default; conversion and the duplicate scan are optional.
  Conversion implies metadata processing, and stages run in order: import, metadata,
  conversion, duplicate scan. Handoffs stay within that import and never drain another job.
  Empty or finished stages are skipped and hide options that can no longer run.
- People scans stay explicit on Imports; the People page shows results.
- The import worker uses a PostgreSQL advisory lock against simultaneous imports, stops at
  batch end for review, and resumes from the existing inventory. Folder expansions need an
  explicitly selected new job.

## Imports page

- **New/Current Import** and **Previous Imports** are separate. Media-copy completion moves a
  batch to Previous Imports even while follow-up work continues; cancelled imports stay in
  history. Collapsed rows name active stages with a green activity dot, and waiting, paused
  or failed face scans stay visible without opening the row.
- Stage status comes from `web/import-status.mjs` (shared with Import overview): live worker
  state first, then completion from item counts, then saved paused/error state.
- Imports first requests `/api/import/history?summary=true`; expanding a row fetches that
  job's history and issues. Concurrent detail requests are coalesced and cached briefly.
- Counts are per import and stage. File progress counts processed items, including duplicates,
  unsupported items and errors (the error count stays visible). Byte progress counts only
  terminal imported, duplicate and unsupported bytes. These are queue totals, not disk usage.
  Elapsed time sums active intervals from durable state events, excluding pauses. Estimates use
  current-run throughput and are approximate. Discover people uses (processed + excluded) /
  total.
- **Import overview** has sortable totals, separate photo/video counts and bytes, start time,
  combined stage state and runtime, and linked warnings. Each stage's warnings are shown
  separately from the import total.
- **Review people** opens People / Review faces scoped to that import. **View in Library**
  opens Library filtered to the import; Library's **Import name** selector does the same and
  combines with search, year, type and pagination.
- Migration 013 stores modification times as exact nanoseconds in `numeric(30,0)`, so early
  Windows timestamps (1601 epoch) don't overflow during inventory.

## Browser uploads

The **Uploads** page sends a folder from the computer running the browser. Browse opens the
native folder chooser; Start uploads supported files recursively and queues a verified import.
Batches show as **Upload · folder name** and server copies as **Server · volume label**. Upload
provenance keeps the relative path (`Holiday/subfolder/photo.jpg`, forward slashes), which is
searchable; the client's absolute path is never disclosed. Empty folders aren't stored, and
ignored or unsupported files aren't transferred (the manifest records them).

**Two stages.**
1. All accepted media is sent to private staging under `data/uploads/<session-id>/` and every
   whole-file SHA-256 is verified. Only after the durable import-job transaction commits does
   the page say **Safe to close the browser or disconnect**. A byte meter at 100% is not that
   confirmation.
2. The normal server import pipeline runs, with no dependency on the browser. A queue
   dispatcher in the web process imports verified sessions when idle, and queued jobs survive
   restarts.

**Transfer.** The page uses a directory file input, Blob slices and an incremental SHA-256
worker, so it needs no secure-context-only APIs. Use desktop Safari, Chrome, Edge or Firefox,
and keep the tab and computer awake until verification finishes. Closing or reloading pauses
the transfer: browse to the same folder and choose **Resume selected folder**. The manifest must
match and content is hashed again. Requests have deadlines (30 s for commands, 120 s for chunks,
15 s for reconciliation). After an ambiguous chunk response the client reads the server's
acknowledged offset; an accepted chunk is never resent, and an unchanged offset allows three
attempts with backoff before pausing. Cancel is a separate confirmed command. The upload helper
follows the selected session through each state and is remembered across a tab refresh.

**Safety.** Uploads are bound to the creating account, require a custom request header and
same-origin checks, and use generated staging identifiers, never client paths. Absolute, drive
or UNC paths, backslashes, traversal and malformed manifests are refused; Unicode names are kept
as data. Streams are capped, chunk and whole-file checksums are checked, and repeated
Start/finalise requests with the same identifiers don't create duplicate imports.

**Limits** (Settings, stored in `app_settings` key `uploads`, defaults in
`app/uploads/__init__.py`): 5,000 files, 20 GiB per upload, 5 GiB per file, 40 GiB staging,
8 MiB pieces, 5 GiB free space kept, 48-hour expiry, one active transfer, 12 MiB/s. Staging quota
counts retained and queued sources and checks real free space. Destination space is checked
without assuming deduplication savings.

**After the import.** Pause, cancellation and faults keep the staged sources. Cancel stops at a
safe boundary and keeps both imported media and remaining inputs. Incomplete transfers expire
into a cancelled, retained state after 48 hours of inactivity; they still use quota, and
discarding them is a separate reviewed decision. Successful cleanup waits for the import and any
requested video stages, then re-verifies every published file's size and checksum (by the
verified upload hash for duplicates) before removing staging. Missing or corrupt destinations
stop cleanup. Client originals and library media are never removed.

## Visibility

Every media item has an owner (`media.owner_name`, empty for imports) and a level:
**shared** (everyone with an account) or **private** (its owner and administrators).

- The normal interface uses shared media for both imports and uploads: upload privacy choices
  and the Library/upload-history privacy actions are hidden. The ownership fields and API
  safeguards stay in place, so nothing previously private is exposed.
- The level is enforced in SQL by `familyfoto_can_see()` (schema 36), bound to the web request
  by `app/viewer.py` when a pooled connection is checked out. Workers, scripts and
  administrators have no viewer and see everything. Request-path queries call
  `media_visible('m')`; `tests/test_media_visibility.py` covers each surface and lists the
  modules that must keep applying the rule.
- A face from a private photo never appears for anyone else, and a person who appears only in
  private photos isn't listed for other viewers. Albums omit hidden items from lists, counts and
  covers. A duplicate group containing a copy the viewer can't see is hidden, and "already in
  library" never names another person's private file.
- An upload's items belong to the uploader. Owners change only their own items; administrators
  change any, and hiding an owner-less imported item records that administrator as owner.
  Changing a level never moves files. An upload matching an existing item stores nothing and
  shows a generic "duplicate", so uploading someone's private photo never grants access to it.
  Another person's upload batch is invisible to non-administrators.
- All roles, including guests, can browse People. Editing People needs an administrator.

## Ignored and unsupported files

The ignore list is in `app/imports/ignore.py`; Settings shows the patterns and an explicit **No
extension** rule. Matching is case-insensitive on the final file-name suffix at any depth, never
on directory names. Ignored files are not read or copied and don't appear in review or totals.
Existing inventory rows stay for audit and are filtered from displayed counts and issue lists.
Server imports and uploads share the same classification.

- Ignored: `*.moff`, `*.modd`, `*.thm`, `*.aae`, `*.db` (including `Thumbs.db`), `*.info`,
  `*.htm`, `*.html`, `*.ini`, `*.vcd`, `*.txt`, `*.log`, `*.bup`, `*.ifo`, `*.zip`, `*.gz`,
  `*.mp2`, `*.dvd`, `*.ses`, `*.spu`, `*.xmp`, `*.pages`, `*.numbers`, `*.dmg`, `*.lrf`, `*.srt`,
  `*.asf`, `*.comicdoc`, `*.doc`, `*.docx`, `*.isf`, `*.lnk`, `*.cb`, `*.cnb`, `.ithmb`, `.jpgz`,
  `.gifx`, `.ais`, `.url`, `.tof`, `.ion`, `.!!!`, `.thumbnail`, `.avif`, the damaged `.jpg*`
  variants (`.jpgr`, `.jpge`, `.jpgs`, `.jpgg`, `.jpgii`, `.jpgd`, `.jpgj`, `.jpgl`, `.jpgss`,
  `.jpgff`, `.jpgvv`, `.jpgf`, `.jpgx`, `.jpgaa`, `.jpga`, `.jpgw`, `.jpgk`, `.jpgyy`, `.jpgu`,
  `.jpgzzz`, `.jpgc`), extensionless names (including a trailing dot), and `.*`.
- `.*` ignores dot-prefixed file names (AppleDouble `._` sidecars, `.DS_Store`), not whole
  directories whose names start with a dot.
- `.psd` stays visibly unsupported. Unsupported files are distinct from failed copies.
- HEIC/RAW and older video codecs are preserved but may lack previews or browser playback.
  Already-imported AppleDouble sidecars are kept and labelled ancillary.
- Missing capture dates stay unknown; folder names are never treated as dates.

## Audio

MP3, M4A/M4B, AAC, WAV, FLAC, OGG/Opus, WMA and AIFF are preserved through the verified
non-photo path with the normal checksum, duplicate, download, album and Trash behaviour. MP2
stays excluded. For compatibility, database `kind` stays `video` for non-photo media, so audio
appears in Library and Videos and in video counts. Processing records
`video_metadata.media_type = audio` from the actual streams (attached cover art isn't a video
stream), skips frame extraction and doesn't report a thumbnail failure. Known audio extensions
show a music icon before processing. Raw AAC mislabelled as MP4 is served with its detected
type. The viewer uses native browser audio controls; nothing is transcoded. Reimport a source to
collect audio skipped by older versions.

## Post-import validation

Never import another source automatically. The source stays read-only in every task.

1. After a batch reaches `completed-review-required` with no pending or error items, run
   `python -m app.jobs.review`. It holds the importer lock, validates queue links and inventory
   bytes, checks every cataloged destination's size and existence, and verifies a spread of
   SHA-256 samples. It repairs nothing and is not a full checksum rescan. Report:
   `data/post-import-review.json`.
2. Check gallery thumbnails, paging, search, year filters, large-image preview and representative
   video playback in a browser, then mark the report `passed`. On a disk or integrity fault,
   stop and review rather than retrying.

**Thumbnail layout.** Previews are written to `data/thumbnails/aa/bb/<sha256>.jpg`; the web server
also reads legacy flat `<sha256>.jpg` files. `python -m app.photos.thumbnails` migrates old
previews after the review report has passed: it takes the importer lock, verifies and hashes each
JPEG, renames it atomically on the same volume and hashes it again, never overwrites different
content, and can be rerun after an interruption. Progress: `data/thumbnail-migration.json`.
Thumbnail creation validates its output and retries a first-frame poster for short videos.

## Portability

Media destination keys (`media.relative_path` and converted-original paths) and new upload
provenance are content-addressed relative paths using `/`, so they are OS-independent. Historical
server `source_root`/`source_path` values, configured absolute roots, Windows volume identities and
executable locations are OS-specific and kept as audit data. Moving a library between computers
or operating systems uses [backup and restore](BACKUP.md).

## API

- `GET /api/import/history`: grouped stages, counts and pipeline status (`?summary=true` for
  collapsed rows).
- `POST /api/import/start`: validated server source and initial automatic options
  (administrators only).
- `POST /api/import/{job_id}/options`: saved video-processing, conversion and duplicate options.
- `POST /api/import/{job_id}/{stage}/resume`: resume import, metadata, conversion or duplicates.
- `POST /api/import/{job_id}/pause`: pause the selected job.
- `GET /api/import/ignore-patterns`: patterns plus extensionless behaviour.

Authentication and origin checks apply. Source changes, configuration changes and migrations need
idle worker reservations. Core modules: `app/jobs/__init__.py`, `app/jobs/job_scope.py`,
`app/imports/importer.py`, `app/video/metadata.py`, `app/video/conversions.py`,
`app/uploads/__init__.py`.
