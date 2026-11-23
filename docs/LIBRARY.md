# Library

Browsing, viewing, Trash and photo duplicate review. Nothing here rewrites an original:
rotations, crops, covers and album order are catalog metadata.

Contents: [Galleries](#galleries) · [Selection and downloads](#selection-and-downloads) ·
[Viewer](#viewer) · [HEIC and HEIF](#heic-and-heif) · [Albums and collections](#albums-and-collections) ·
[Trash and permanent deletion](#trash-and-permanent-deletion) ·
[Photo duplicates](#photo-duplicates) · [Interface components](#interface-components)

## Galleries

Library, Photos and Videos share one paged gallery (also used by albums and Trash), with
search by file name and source path, year, import and media-type filters. Pages hold 50, 100,
200, 500 or 1,000 items (default 1,000; People review pages default to 100). The size is kept
in page memory only; changing it returns to page one and clears the selection. The media API
accepts 1 to 1,000. Thumbnails keep a fixed aspect ratio; videos play in the browser with byte
ranges.

## Selection and downloads

Library, Photos and Videos offer **Select**, **Select page**, **Clear**, **Download** and **Move
to Trash**. Counts refer to the loaded page, never unseen matches, and changing page, search,
year, import or view clears the selection.

- Desktop: drag a rectangle across thumbnails (edge scrolling extends it); Ctrl/Cmd-drag toggles,
  Shift-drag adds, Shift-click selects a range. Keyboard: Tab to a tile, Space/Enter toggles,
  Ctrl/Cmd+A selects the page, Escape exits. Touch keeps native scrolling with tap selection.
  Select mode suppresses native text and image highlighting.
- **Move to Trash** uses the per-item permission and maintenance checks, stops at the first
  failure and keeps the rest selected. Guests can select and download but not move or delete.
- A single **Download** saves the original file directly. Several items open a download summary;
  after the files are checked, **Download ZIP** produces one attachment. The selection is
  captured when the summary opens. Preparing can be cancelled; after that, use the browser's
  Downloads.
- The ZIP holds the same unmodified bytes as individual downloads, named by sanitised original
  stem and extension plus `[media ID]`. ZIP64, stored without recompression, streamed in 64 KB
  chunks, at most 1,000 items, and never buffered in browser memory or a server temp file.
  Missing files or active conversion/deletion locks stop the preflight; a mid-transfer disk
  failure gives an incomplete browser download, never a finalised partial ZIP.
- Download tickets expire after ten minutes and are bound to the signed-in account. Shared
  content locks protect the originals during transfer. Recently Deleted items can be downloaded
  without restoring them.

## Viewer

The viewer fills the browser window, with previous/next at the sides and information, trash and
close controls over the image. Capture time stays visible; technical details open separately.

- **Rotation:** saved in 90-degree clockwise steps for photos and videos; applies to the viewer
  and thumbnails.
- **Crop:** drag or resize a selection, then Save crop; Reset crop restores the full image.
  Crops are normalised against the EXIF-oriented image before rotation, so later rotations keep
  the selected content. Downloads stay uncropped. Uses vendored Cropper.js 1.6.2 (MIT,
  `web/cropper-LICENSE.txt`).
- **Zoom (photos):** 100–800% of the fitted image; drag to pan. `+`/`-` zoom, `0` fits,
  Shift+arrows pan, plain arrows still navigate. On touch, tap zoom-in first, then drag or pinch.
  Zoom resets when changing photos and never requests a larger original.
- Video playback controls are described in [Video](VIDEO.md#viewer).

## HEIC and HEIF

`requirements.txt` pins `pillow-heif` alongside Pillow; the PyPI wheels include libheif, so no
OS codecs are needed. `app.library.media` registers the plugin in every importer and API process.

Imports keep the original HEIC/HEIF bytes and create a JPEG thumbnail (640 × 480 box) and an SDR
browser preview (2560 × 2560 box) under `data/thumbnails` and `data/previews`. The viewer shows
the preview; Original and Download return the HEIC file. libheif applies container
orientation once, embedded ICC profiles (including Display P3) are converted to sRGB, and HDR
data stays in the original. Decoding keeps libheif's security limits, caps sources at 128 MiB /
80 MP and ICC profiles at 4 MiB, uses two threads and skips auxiliary and depth images. Corrupt
or oversized files keep a warning and their original.

To repair missing derivatives once uploads, imports and video work are idle, run
`python -m app.photos.repair_heif --ids 123` or `--all-heif`. It uses the pipeline and content
locks, verifies the original before and after, keeps existing dates and dimensions, runs each
decoder in a child process with a 45-second deadline, yields to a new upload, skips completed
items on rerun and records results in `data/heif-repair.json`.

## Albums and collections

Albums, collections, covers and their ordering are described in [Albums and collections](ALBUMS.md).

## Trash and permanent deletion

**Recently Deleted** is a catalog-only trash view. Moving an item there keeps its destination
file and import history; Restore removes the trash entry. There is no automatic emptying.

- **Who sees what:** administrators see every deleted item. Other accounts see only items they
  moved to Recently Deleted themselves, while the media is still shared with them or theirs. The
  same rule guards direct media access and restore. Schema 37 records `media_trash.deleted_by`
  (the first deleter's username; older and system deletions have none and stay admin-only).
  Deleting a shared item from Library still removes it for everyone; there are no per-user trash
  copies.
- **Permanent deletion** is administrator-only, from Recently Deleted, with confirmation. It
  removes the active destination copy and any retained converted original, then its thumbnails
  and previews, then sets `media_trash.deleted_at` and triggers face, People and album cleanup.
  Source media is never touched.
- **Crash safety:** a durable `deleting_at` intent is committed before any unlink. If anything
  fails (including a cache unlink, which returns 409), the item stays in Recently Deleted, can't
  be served or restored, and a retry finishes the work. A missing file without a prior intent,
  or a checksum fault, stops deletion for review.
- **Concurrency:** Trash, restore and permanent deletion work during imports. They share the
  maintenance lock with imports, and a per-content PostgreSQL session lock serialises import
  copy/catalog work, thumbnail migration and HEIF repair with deletion of the same hash. Video
  maintenance and decoder leases block Trash changes. Disk and library identity checks and
  checksum verification come first.
- **Caches** are identified by the full 64-character SHA-256 (never the shard prefix). Both flat
  and sharded layouts are removed, including attributable partial outputs. Cleanup never deletes
  recursively and refuses links, junctions and paths outside the data directory. It is not a
  secure-erase facility.
- Catalog and audit records stay after deletion, so a byte-identical file in a later import is
  recorded as a duplicate and never recreated.

Icons are vendored Lucide 0.468.0 (ISC, `web/lucide-LICENSE.txt`).

## Photo duplicates

A per-import **Photo duplicate scan** (Imports) finds photos with identical pixels and can move
clear duplicates to recoverable Trash. It runs after import, adds nothing to the import path,
and never involves face recognition. Whole-file SHA-256 import matching is unchanged.

### Running

- Start / Resume and Pause use the pipeline reservation and exclusive media-operation lock.
  Automatic scanning after import is optional per import (off by default). Start is disabled
  when nothing is pending.
- Only the selected import is scanned (its original photos and retained duplicate memberships),
  compared with already fingerprinted eligible photos from other imports; other imports are
  never scanned silently. A shared media ID is fingerprinted once. Checkpoints and active time
  survive pause and restart.
- Only verified destination files are read, with disk identity and library marker checks.
  Size or checksum failures stop the worker for review. Decode, format and size problems are
  per-item warnings. Dot-prefixed legacy entries are excluded.
- `Move clear duplicates to Trash` is on by default per import and is applied at resume, every
  100 items and at completion. Turning it off stops later groups but doesn't restore earlier
  ones.

### Matching

- JPEG only in the normal scan: at most 80 MiB and 60 megapixels, no multi-frame JPEG/MPO, and
  above 40 MP the estimated decode allocation must fit in half of available memory.
- Full-resolution pixels are decoded with EXIF orientation applied, then hashed together with
  dimensions, colour mode, ICC profile identity and EXIF colour evidence. Different ICC profiles
  don't match. A second key over the JPEG image data (excluding EXIF APP1 and comments) labels a
  group as image-data matching when both keys agree.
- Excluded: active crops or manual rotations, trashed media and stale fingerprints. Resized,
  recompressed or merely similar images are not matched.
- The fingerprint version includes the Pillow version; a mismatch re-fingerprints only when that
  import is explicitly started.

### Automatic Trash

Only exact current-version pixel matches qualify. Non-empty capture date, camera and GPS values
must not conflict, and the keeper must retain all of them; ties keep the earliest catalog entry.
A confirmed decision uses its chosen keeper. Keep separate, Reset decision, stale confirmations
and restored groups are protected. Only members of the selected import can be trashed by its
scan. Trashing and its audit entry commit together; files, hashes, ownership, albums and source
references are untouched.

### Review

**Review matches** shows each import's groups (20 per page, up to 100 warnings) with thumbnails,
original links, size, capture date, camera, orientation, GPS and an EXIF fingerprint. Earliest
catalog creation is the initial preferred copy. Actions are Confirm duplicates, Keep separate
and Reset decision; decisions use optimistic revision and member-list checks, and a new member
invalidates an old confirmation.

**Trashed duplicates** shows kept and trashed photos side by side with Restore and
administrator-only permanent deletion. Selection is limited to the current page (at most 40).
Bulk deletion confirms the count and deletes sequentially, stopping at the first error. Restoring
by either path protects the group from being trashed again.

### Extended scans

- **Scan large image** (on the over-60-MP warning) requeues those photos and lifts only the
  megapixel cap; the other limits stay. Results are review-only. After a recorded failed
  attempt, **Reviewed - skip** stores a durable `skipped` state.
- **Scan unexamined photos** (import warning details) requeues eligible warning rows with an
  extended policy: single-frame JPEG, PNG, WebP, BMP and TIFF by detected format, 80 MiB, 60 MP
  and the memory check. MPO files compare their primary image only, recorded as
  `primary-image-only`. For the two "broken data stream" and "image file is truncated" errors,
  JPEG decoding is retried in a separate process with Pillow's tolerant decoder (120-second
  timeout), marked `decode_mode=tolerant-jpeg` with a damaged-pixel warning. All extended results
  are `review_only` and are never trashed without a fresh confirmation.
- Imports retry those two errors for thumbnail creation only (actual JPEG and PNG content) in an
  isolated child, keeping a warning that the original is damaged.
- `python -m scripts.rebuild_missing_thumbnails` audits missing thumbnails across the catalog;
  `--apply` repairs and `--ids ID ...` limits the scope. It needs idle workers, takes content
  locks, verifies originals before and after, and writes a dated report under `data/`.

Migrations 020 (fingerprints, checkpoints, decisions, status) and 022 (automatic Trash option and
audit).

## Interface components

The interface is vanilla HTML and ES modules with locally vendored **daisyUI 5.7.50** (MIT,
`web/daisyui-5.7.50.css`, SHA-256
`6a2cf91a0e9a7da09cf8109c45c3fae171e8967122576043f2b94da47fdf6b64`, licence
`web/daisyui-LICENSE.txt`). There is no Tailwind build, CDN request, external font, telemetry or
runtime download. `web/ui.mjs` applies component classes to native controls; labels, focus,
dialogs and permissions are unchanged, and viewer buttons keep accessible names and 44 px targets.
