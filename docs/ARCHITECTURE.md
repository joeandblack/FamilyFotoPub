# FamilyFoto Architecture

## Storage and identity

Windows hosts the API/workers and PostgreSQL. Browser clients use the API, not
database or filesystem credentials. Source paths and enrolled disk identities are
pinned per import. Maintenance reads verified destination copies, not source disks.
D: contains runtime state, staging and thumbnails.

Whole-file source SHA-256 remains the deduplication identity. Conversion records a
separate stored SHA-256 and original path/size without changing import ownership.
All imports retain counted duplicate audit rows without secondary media links.
See [Import](IMPORT.md#duplicates-and-ownership).

## Admission and recovery

Import inventories eligible files before copying, skipping links/junctions. It
hashes source bytes, writes a generated `.partial` destination, flushes/fsyncs and
reads the copy back for checksum verification. Source size/mtime and disk identity
are checked before catalog commit. Published but unindexed copies are verified on
resume; corrupt final files are never silently overwritten. Media uses the
two-level `aa/bb/<sha256>.<extension>` layout; filenames/dates live in the catalog.

Database queue/events are authoritative; status JSON is only a presentation
snapshot. Worker heartbeat freshness uses monotonic time. Logs, staging evidence
and private paths remain local and excluded from Git. No source-folder cleanup or
changes to the separate old FotoManager application are part of this system.

Import, metadata/posters, conversion and photo duplicate scans share reservations and operate on
one selected import. Post-processing requires completed prerequisites. Optional
automatic handoffs remain in the same import. Pause finishes the current file;
resume reads durable database state. Decoder leases and fault markers are never
cleared by routine recovery. Disk/checksum/decoder faults require review.

Discover people is lower-priority background work. Its durable scan reservation
prevents a second scan, but maintenance/content leases are held only around the
current photo. Foreground handoffs are reserved before launch; People yields at
the next checkpoint and waits automatically. Browser viewing alone does not pause
the scan. Explicit pause and faults still require an explicit resume.

## Conversion and retention

An explicit registry dispatches AVI, FLV and MTS handlers. Codec planning is separate
from AVI policy; staging, validation and publication are shared. Legacy AVI runtime
filenames remain for compatible resume. FLV/MTS originals cannot be automatically
removed by conversion. See [Video](VIDEO.md#conversion).

Gallery membership is catalog-based; photo edits are non-destructive settings.
Trash is reversible. Explicit permanent deletion verifies hashes/identities and
rejects shared catalog paths. It can remove a converted item's retained destination
original; this is distinct from automatic conversion cleanup.

## Photos and People

Photo duplicate review is implemented as a post-import stage. Conservative matches
can be recoverably trashed; extended/recovered-image matches require manual review.
Permanent deletion is a separate confirmed operation. See [photo duplicates](LIBRARY.md#photo-duplicates).

Missing thumbnails can be rebuilt from verified destination copies. Tolerant
JPEG/PNG display recovery is isolated in a bounded subprocess and never relaxes
ordinary duplicate fingerprints. Original hashes remain unchanged and recovered
images retain damage warnings.

The single `people` catalog keeps reviewed People identities, receipts and corrections,
keyed by face rather than by vector, so they survive a model change or a move to another
computer. Faces are processed by local InsightFace or FaceNet providers in separate
processes. See [People](PEOPLE.md). Video faces and cross-container video duplicate
scanning are not built.

## Responsive reconciliation

Design principle: routine background changes are the application's responsibility,
not a reason to block the user, expose internal concurrency details, or ask for a
manual page refresh. People bulk review is the implemented reference for this
pattern; it is not a claim that every older operation already uses it.

- Capture the exact selection the user saw. Never silently include newly arrived
  items in a submitted action.
- Revalidate under the appropriate server lock. Process still-valid selections,
  preserve newer edits, and reconcile changed items without rejecting the whole
  batch for an unrelated change. Do not blindly replay writes against fresh data.
- Show immediate, non-modal progress. Stream real counts and periodic heartbeats
  during long work; distinguish processing from saving and report completion only
  after commit. Disable duplicate submissions, not browsing or the whole page.
- Return affected cards and destinations with the result. Update them in place,
  retaining unchanged thumbnails, current pagination and remaining visible
  selections. Use a short neutral status such as "Updated", not technical conflict
  explanations or refresh instructions for routine selection changes.
- Do not hide real failures or claim an uncertain write succeeded. A lost
  connection is different from a reconciled selection; never automatically repeat
  an operation whose commit outcome is unknown. Current People streaming does not
  provide durable operation tracking after a browser disconnect.

Acceptance checks include scan growth on an earlier page, concurrent moves and
ignores, a removed target, repeated requests, post-commit completion, unchanged DOM
nodes, and desktop/mobile pagination. See [People](PEOPLE.md#people-pages)
for the implemented boundaries and recovery behavior.
