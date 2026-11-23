# FamilyFoto

A private photo and video library hosted on a Windows PC or an Apple Silicon Mac with macOS 27 or
newer, accessed through a browser. PostgreSQL stores the catalog; media stays on the enrolled
destination disk. Import source files are never modified.

## Import and processing

- Import a server folder or upload from another computer's browser.
- All imports skip exact duplicates, retaining counts without adding another
  library association. No import mode selection is needed.
- Each import owns its video metadata/poster processing and video conversion.
  Start, Pause and Resume act on that import; automatic handoffs are optional.
- Import, video processing, conversion and photo duplicate scans are serialized.
  Discover people runs at lower priority and yields between photos to these jobs.
  Disk identity and checksum checks remain mandatory; fault recovery is deliberate.
- AVI, FLV and MTS can become browser-friendly H.264/AAC MP4s. FLV/MTS choose
  remux, audio-only conversion or full encoding from the actual codecs.
  **Destination FLV/MTS originals are retained during conversion.** Playback review
  lets an administrator explicitly delete selected retained copies after confirmation.
  Keeping an original leaves it available for later review. AVI cleanup is separately controlled.
- Converted downloads use `.mp4`, including ZIP downloads, while the catalog keeps
  the original name and source hash for provenance and duplicate imports.
- FLV is supported. Older unsupported FLV records are not automatically
  retried, and previously excluded files must not be assumed copied or backed up.

## Library

Photos and videos share a searchable library, thumbnails and albums. The viewer
supports downloads, non-destructive photo adjustments and recoverable Trash.
Photo/video slideshows preload upcoming photos. Compact selection, latest-action
Undo and consistent confirmation dialogs support bulk library operations.
Permanent deletion requires an explicit authorized action. Keep source archives
and independent backups; a completed import may still have unsupported files.

Discover people starts from each completed import. People displays shared results,
review queues, relaxed possible-match suggestions and paged group photos with
bulk corrections and album actions. Native InsightFace and FaceNet providers run
in local Python processes without Docker. People actions share compact progress
feedback, using actual counts when available and an animated indicator otherwise.
Import overview combines AVI/FLV/MTS counts in one sortable Conversion pending column
with a per-format hover breakdown.

Photo duplicate scans can move confidently identified duplicates to recoverable
Trash; uncertain or recovered-image matches require review. Scan unexamined photos
also rebuilds missing thumbnails. Future imports recover damaged JPEG/PNG thumbnails
where safe, retaining damage warnings and leaving originals unchanged.

## Documentation

One document per topic, each describing what the code does now. If a document and the code
disagree, trust the code and fix the document.

| Document | Covers |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | How the system fits together: storage identity, admission and recovery, conversion, People, responsive reconciliation |
| [Import and upload](docs/IMPORT.md) | Imports and browser uploads, duplicates and ownership, per-import stages, the Imports page, visibility, ignored files, audio, post-import validation |
| [Library](docs/LIBRARY.md) | Galleries, selection and ZIP downloads, the viewer, HEIC, Trash and permanent deletion, photo duplicate scans |
| [Albums and collections](docs/ALBUMS.md) | Albums, nested collections with live People links, covers and framing, ordering, visibility, Trash behaviour, schema and API |
| [Video](docs/VIDEO.md) | FFmpeg, metadata and posters, AVI/FLV/MTS conversion, playback review, staging cleanup, recovery |
| [People](docs/PEOPLE.md) | Face providers and setup, the `people` catalog, moving to another computer, calibration, Discover people and the People pages |
| [Backup and restore](docs/BACKUP.md) | Backup password, backing up and test restores from Settings, Standard and Fast, restore and activation, moving a library, schema upgrades, every command |
| [Operations](docs/OPERATIONS.md) | Network access, HTTPS, accounts and sessions, PostgreSQL, removable disks, desktop launcher, status indicator |
| [Development](docs/DEVELOPMENT.md) | Code layout, running from a checkout, tests, branching and CI |
| [Install acceptance](docs/INSTALL_ACCEPTANCE.md) | Third-party software and face-model terms you accept by installing |

Installing:

- Windows setup preview: `powershell.exe -NoProfile -File installer/windows/install.ps1 -Mode Plan`.
- Mac setup preview: `bash installer/macos/install.sh plan` (run as the signed-in user, not sudo).
- [Installers](installer/README.md): new libraries, restore and optional native People, the
  shared installer engine and [release packaging](installer/PACKAGING.md). Follow the platform
  guide ([Windows](installer/windows/WINDOWS_INSTALLATION.md), [Mac](installer/macos/README.md))
  for prerequisites, permissions and interruption recovery. Catalog upgrades are separate from
  application-code updates; never run a fresh installation over an existing library.

Elsewhere: [the People package](face_detect/README.md), the
[Fast backup white paper](app/fastbackup/WHITE_PAPER.md) and [operator scripts](scripts/README.md).
Open work is tracked in [GitHub issues](https://github.com/joeandblack/FamilyFotoPub/issues)
([TODO.md](TODO.md) points there); [People](docs/PEOPLE.md#not-built-yet) and
[Backup](docs/BACKUP.md#not-built-yet) also list what isn't built yet.

Visual duplicate comparison never replaces exact SHA-256 matching during import.
People requires explicit local service configuration; cloning the repo does not
copy recognition data, credentials, media or opt-in settings.

## Backup and installation status

Settings offers Standard and Fast backup with full verification. Fast (where the installation
enables it) is the default selection. Standard supports retrying interrupted copies; Fast copies
first and then verifies, and an interrupted attempt starts a new copy. Both stop FamilyFoto while
capturing the library; keep the backup disk connected. The backup's catalog is protected by the
administrator password used when it was created, which is also needed for restore. Media on the
backup disk is not encrypted; follow the destination privacy warnings. See
[Backup and restore](docs/BACKUP.md) and the platform restore guide before moving a library
between Windows and Mac.

The shared online-first launcher and offline bundle tooling in
[installer/common](installer/common/README.md) are under development. They are not
yet a qualified downloadable installer release. Use the current platform guides;
clean-machine qualification, release assets and distribution materials remain
release gates. FamilyFoto itself does not require Docker.

## Development

Import filtering combines explicit exclusions with recognized non-media document/
text extensions from Python's built-in MIME map and temporary-save filename rules.
Unknown formats remain reviewable; this is not a claim of content validation from
an extension. Historical ignored entries are hidden from review, not deleted from
the source disk or erased from import history.
PDFs remain in Unsupported files for user review because they may contain photos.
They are not automatically ignored, converted, or deleted; PDF-to-image conversion
is not currently implemented.

Use the existing virtual environment and isolated PostgreSQL test schemas.
Integration tests require `FAMILYFOTO_INTEGRATION=1`. Never use production media
for destructive tests. Keep secrets, runtime status, reports and private media
out of Git. Setup and test commands are in [Development](docs/DEVELOPMENT.md).

## License

FamilyFoto is licensed under the [GNU General Public License v3.0](LICENSE). Third-party
software and face models installed with it keep their own terms; see
[Install acceptance](docs/INSTALL_ACCEPTANCE.md).
