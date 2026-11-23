# Video

Video processing, conversion and playback. Every stage reads verified destination copies,
never the source disk. Source media stays read-only, and the completed source disk may be
ejected and kept as the full-quality original archive. Imports still need both enrolled disks.

Contents: [Tools](#tools) · [Metadata and posters](#metadata-and-posters) ·
[Conversion](#conversion) · [Playback review and retained originals](#playback-review-and-retained-originals) ·
[Staging cleanup](#staging-cleanup) · [Recovery](#recovery) · [Viewer](#viewer) ·
[Names and downloads](#names-and-downloads)

## Tools

On Windows, `installer/windows/install-ffmpeg.ps1` provisions the pinned FFmpeg essentials
build 9.0.2 under `runtime/ffmpeg/bin`, from the distributor linked on
https://ffmpeg.org/download.html (https://www.gyan.dev/ffmpeg/builds/). The script checks the
archive SHA-256 before extracting it:
`60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`. On the Mac, the installer
provisions its own bundled FFmpeg. Runtime binaries and downloaded archives are not in Git; keep
their license and source references with the installation. Nothing changes the global PATH or
services. Update the pinned version and hash deliberately when the vendor ships security fixes.

Alternatively, set `video_tools.ffmpeg` and `video_tools.ffprobe` in private configuration to
trusted local executables.

## Metadata and posters

The metadata worker writes duration, dimensions, codecs and container/stream creation-time
tags to PostgreSQL, and JPEG posters into the hash-sharded thumbnail store.

**Running it.** Import validation and thumbnail migration must have passed first. Choose a
completed import in Imports and select Video processing > Start / Resume. The command-line
equivalent is:

```powershell
.\.venv\Scripts\python.exe -m app.database
.\.venv\Scripts\python.exe -m app.video.metadata --job <import-job-id>
```

- The pipeline holds one exclusive reservation across stage transitions, so only one operation
  runs. See [Import](IMPORT.md#per-import-processing) for the shared controls.
- The worker runs at Windows background CPU and I/O priority and processes one file at a time.
  FFmpeg child tools use idle priority, one decoding thread and limited probing. Only
  supported local demuxers are accepted, never playlists or network input.
- Before each extraction the whole destination file is SHA-256 verified again, with per-chunk
  disk guards. Disk IDs and the library marker are monitored, and size and mtime are rechecked
  after reads.
- A restart handles only pending rows; done, ancillary and warning rows are kept. Video work
  belongs to the first successful import; later exact duplicates keep provenance but add no
  work or counts.
- Pause finishes the current file. Resume in Imports checks locks, prerequisites and
  decoder/fault markers. Command-line entry points honour pause flags (`data/VIDEO-PAUSE`).
  There is no unattended startup, automatic fault recovery or cross-import handoff.

**Crash safety.** Each poster is verified, flushed and atomically published before its row is
committed; a crash before commit leaves the row pending, and a rerun regenerates only the JPEG.
Each decoder's PID is recorded before waiting and cleared after it exits, so a new worker never
starts an overlapping decoder. Disk, checksum and database faults pause the worker. A decoder
running over 60 seconds is never killed: the job pauses and records
`data/video-decoder-busy.json`. Inspect that process and the disk before clearing the marker.

**Dates and warnings.** Date provenance and the raw timezone are kept. Dates with an explicit
offset are normalised to UTC for filtering; others stay unspecified, and existing dates are
never overwritten. A creation tag may be encoding time rather than shooting time, and the viewer
says so. Browser compatibility is an inference from codec and container, not a playback
guarantee. AppleDouble `._` sidecars are identified by name **and** binary header and labelled
ancillary. Other decoder or poster problems become video warnings, separate from copy failures;
warnings are not discarded or retried automatically. A missing or mismatched original pauses
the worker and is never overwritten.

**Status and progress.** Runtime status is `data/video-status.json`; diagnostics rotate in
`logs/video.log`. Imports shows done/warning/sidecar/pending counts, the running phase, active
time per import (paused time excluded) and a remaining-time estimate from the current run, every
five seconds. Percentages count files, not bytes. Front-end calculation tests:
`node --test tests/video-progress.test.mjs`.

## Conversion

Conversion is a separate stage under Imports > selected import > Video conversion, available
once that import and its video processing have finished. It applies to AVI, FLV and MTS
(`.m2ts` is importable but not converted). Private configuration `avi_conversion.enabled` gates
the stage and `avi_conversion.remove_destination_avi` controls AVI cleanup; both default to
false. Enrolling MTS files never starts conversion by itself, and automatic idle launching is
not implemented.

Imports shows a sortable **Conversion pending** count per import, with a per-format breakdown on
hover. It counts unconverted and published-but-unfinished items, excluding Trash, ancillary
media and terminal outcomes.

### Codec plans

`app/video/converter.py` dispatches on the catalog original's extension. The shared lifecycle is
in `app/video/avi.py`; `app/video/flv.py` and `app/video/mts.py` supply their own commands and
pinned plans.

| Input | MP4 output |
| --- | --- |
| AVI | H.264 CRF 20, medium preset, two encoder threads, yuv420p, AAC 128 kbit/s, faststart |
| H.264 yuv420p + AAC (or no audio), FLV/MTS | Stream copy |
| H.264 yuv420p + other audio (for example AC-3), FLV/MTS | Copy video, encode AAC |
| Other decodable video, including VP6 | H.264 CRF 20/medium; copy AAC or encode other audio |

Resolution and timing are unchanged (no forced frame rate). This is lossy conversion, not
archival equivalence. Odd dimensions, multiple streams, display rotation and ambiguous streams
need review instead of silently dropping tracks. FLV data streams are not mapped; the retained
original keeps what MP4 cannot represent.

### Checkpoints and integrity

- `data/avi-job.json` and `flv-job.json` pin the encoder/verification profile, cleanup choice,
  and destination, library-marker and database identities (no password). MTS has its own
  versioned profile. A resume with changed settings or identities is refused.
- PostgreSQL per-media states are authoritative: done/warning/duplicate rows are terminal,
  published rows verify the active MP4 and finish cleanup, untouched rows start conversion.
- The worker verifies the original's checksum, stages a verified local copy, converts at idle
  priority, validates the output, hashes it, copies it to its content-hash path and verifies
  the read-back. Space guards require a 5 GiB reserve plus staging and publication headroom.
- Migration 003 keeps `media.sha256` as the original source identity for deduplication and
  thumbnails; `stored_sha256` identifies the active MP4. Original path and size stay recorded.
  Review and duplicate checks use the active storage hash.
- Pause takes effect at file boundaries (`data/AVI-PAUSE`; the `avi-*` names are internal
  identifiers for the shared stage). Faults stay blocked for review. Status is in
  `data/avi-status.json` and `logs/avi-conversion.log`. Discover people yields to conversion
  between photos.

### Validation

Every conversion checks stream counts, H.264/AAC codecs, resolution and duration (within 0.2
seconds), and verifies the published MP4's bytes.

- **Full** validation (the default, and always used for AVI) also compares decoded video frame
  counts and strictly decodes the complete output, audio included, treating any error as a
  failure. Decoder operations have a one-hour review timeout and are never killed.
- **Header-count disagreement:** if both headers report frame counts that differ, each file's
  video is strictly decoded to a null sink with timestamp passthrough and the native time base,
  and the decoded totals must match. Packet counts are never substituted. Accepted evidence is
  saved in the staging `frame-validation.json` and in media metadata.
- **Quick** validation applies only to MTS and FLV, whose originals are always kept. It skips the
  full decoding passes but keeps stream, codec, dimension, duration and checksum checks; known
  frame-count disagreements still need review. A quick conversion counts as done, and the skipped
  playback check is recorded for [playback review](#playback-review-and-retained-originals).

Execution settings are separate from the pinned codec profile:

```json
"conversion_performance": {
  "io_mibps": 0,
  "retained_original_validation": "quick"
}
```

`io_mibps: 0` removes artificial I/O sleeps; a positive value caps combined read and write
throughput. The staged copy is checked against the catalog hash before FFmpeg opens it. Output
metadata records validation mode, checksum assurance, I/O cap and preparation time. Recovery
follows the per-file review requirement recorded at publication, even if settings changed later.
Stop and resume conversion to apply a policy change. Imports shows the current phase (copying,
creating MP4, validating, publishing).

### Retention and cleanup

- Catalog publication commits before any cleanup. With AVI cleanup enabled, both MP4 and AVI
  hashes are checked again before removing only the enrolled destination AVI. A crash after
  publication retries cleanup; a crash after deletion recognises the published MP4.
- FLV and MTS destination originals are always kept, whatever the AVI setting.
- Warnings keep the original. A missing converted MP4 needs manual review; the AVI is never
  substituted automatically. Source-disk files are never modified, and conversion never changes
  import success counts.
- An FLV/MTS output that collides with another catalog path needs review. AVI uses its existing
  output-duplicate Trash policy.

## Playback review and retained originals

An import's **Review playback** link opens a gallery of its converted videos whose managed
originals are still kept, including ones previously marked reviewed. It shows 100 thumbnails
per page; click one to play the MP4. Opening or playing never deletes anything.

Select videos (or Select all, across pages) and choose **Delete retained originals**. This needs
an administrator and a confirmation, verifies both the MP4 and the original, and removes only the
managed retained copy, never a source-folder file. Progress shows the filename and count. A
durable deletion intent makes an interrupted request retryable. Unselected or failed items stay
listed. Permanent deletion from Trash is a separate operation that can also remove retained
originals.

## Staging cleanup

`data/conversion-temp/<media-id>` is scratch space, not a second library.

- A successful conversion, including a resumed one, removes converter-owned scratch files
  (earlier attempts, plans, reports, temporary writes) before it is marked complete. Evidence the
  application needs stays in video metadata.
- A decoder or validation error removes scratch only after the original passes its size and
  SHA-256 check and the storage guard; the error is still recorded as a warning.
- Disk errors, timeouts with an outstanding decoder lease, changed originals and unsafe paths
  keep the evidence and stop cleanup.
- Permanent deletion checks staging paths first and removes staging before recording the
  deletion as complete, so a failure stays retryable.

Cleanup accepts only a positive integer media ID and known converter file names. It refuses
links, junctions and reparse points, hardlinked files, subdirectories and unknown entries, never
deletes recursively, and never touches `conversion-recovery` records or other media IDs.

## Recovery

Reboot recovery is manual. Confirm no worker or decoder remains, take all pipeline and
maintenance locks, and keep the old status and decoder lease. Archive an interrupted MP4 rather
than publishing it without the normal checks, then resume through the import's API. Terminal
records are not retried.

To keep one item's destination AVI through a successful conversion, place a reviewed
`data/conversion-recovery/<media-id>/retain-original.json` containing exactly `media_id` and the
original `sha256`. A mismatched record stops cleanup. The hold stays until a deliberate cleanup.

## Viewer

Videos support the same saved 90-degree clockwise rotation as photos. Rotation is catalog
metadata only; stored media, checksums and downloads are unchanged. Guests can download but
cannot save rotations. The volume icon opens a slider on hover or click (arrow keys adjust,
Escape or clicking elsewhere dismisses). Playback stops when you navigate away or close the
viewer. The Videos page is a gallery; processing controls live in Imports.

## Names and downloads

The catalog keeps the original file name, hash and import ownership after conversion. Single and
ZIP downloads of a published conversion use the original stem with `.mp4`; unconverted files
keep their extension.
