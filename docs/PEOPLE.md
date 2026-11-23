# People

Face detection, recognition and the People pages. Faces are processed locally by native
providers in separate Python processes; nothing is sent to a service, and web handlers never
load machine-learning libraries. The code is in [`face_detect/`](../face_detect/README.md).

Contents: [Principles](#principles) · [Setup](#setup) · [Providers and profiles](#providers-and-profiles) ·
[The People catalog](#the-people-catalog) · [Moving to another computer](#moving-to-another-computer) ·
[Matching policy and calibration](#matching-policy-and-calibration) · [Discover people](#discover-people) ·
[People pages](#people-pages) · [Blurry faces](#blurry-faces) · [Evidence compaction](#evidence-compaction) ·
[Legacy data](#legacy-data) · [Not built yet](#not-built-yet) · [Diagnostics and tests](#diagnostics-and-tests)

## Principles

1. **Meaning is separate from machinery.** What a vector means (its *embedding space*) is set by
   the model and its input rules. Where it was computed (CPU, CUDA, CoreML, Windows, Mac) is
   recorded, never keyed.
2. **User decisions never depend on vectors.** Names, assignments, "not this person" and ignored
   faces point at a face, not at a number computed from it.
3. **A face has a lasting identity** that survives rescans, a new model and a move to another
   computer.
4. **Compatibility is proven, not assumed.** A computer writes vectors into a space only after
   it passes a check against that space.
5. **No silent substitution.** If the configured engine is unavailable, scanning stops and says
   so; browsing, names and decisions keep working. No implicit downloads, provider fallback or
   hardware substitution.
6. **The application depends on an interface.** Providers sit behind one contract
   (`face_detect.service.create_service(config)`, `analyze(image, include_embeddings=True)`) and
   a bounded process protocol (`providers/process.py`).

Optional ML dependencies are imported lazily: importing, browsing and storage work when a
provider or model is absent.

## Setup

For a fresh installation use the platform installer's People option (Windows:
[installer/windows](../installer/windows/README.md#native-people); Mac:
[installer/macos](../installer/macos/README.md)). It checks database ownership and an empty
library first. The lower-level maintainer commands are:

```sh
python -m face_detect.cli.setup --provider insightface --research-only --activate
python -m face_detect.cli.setup --provider facenet --research-only     # optional second provider
python -m face_detect.cli.doctor [--profile <profile>] [--image /absolute/test/photo.jpg]
```

- Setup is an explicit network step: it downloads official release assets, verifies pinned
  checksums, creates separate provider environments under `runtime/face_detect/` (FaceNet's
  Pillow constraint conflicts with the main app), and registers the profile. Only `--activate`
  changes the active profile. Inference never downloads anything.
- Dependency locks are per platform (`face_detect/config/dependencies/*-macos-arm64.txt`,
  `*-windows-amd64.txt`); Linux isn't packaged.
- **Model terms.** The InsightFace code is MIT, but its pretrained buffalo_l models, like the
  FaceNet VGGFace2 weights, are licensed for **non-commercial research only**. Provenance and
  SHA-256 checksums are in `face_detect/config/research-models.json`; review the upstream terms
  ([InsightFace](https://github.com/deepinsight/insightface/tree/master/python-package),
  [facenet-pytorch](https://github.com/timesler/facenet-pytorch)) and
  [INSTALL_ACCEPTANCE](INSTALL_ACCEPTANCE.md) before use.
- To use People: start the **Face detection service** in Settings
  ([Operations](OPERATIONS.md#face-detection-service)), then start Discover people on a
  completed import in Imports. Starting the service never scans.

## Providers and profiles

| Profile | Provider | Model | Alignment |
| --- | --- | --- | --- |
| `windows_insightface`, `mac_insightface` | InsightFace | buffalo_l | five landmarks |
| `windows_facenet`, `mac_facenet` | FaceNet | InceptionResnetV1 VGGFace2 + MTCNN | box crop |

Both return boxes, detection scores, landmarks when supported, and normalised 512-dimension
vectors in the coordinates of the canonical, EXIF-oriented image. Capabilities are explicit:
a detection-only provider can't satisfy an embedding request, and invalid, non-finite or
mis-sized output fails closed. Detection confidence and identity similarity are different
quantities.

```
face_detection:
  platform_defaults: {darwin: mac_insightface, win32: windows_insightface}
  active_profile: null
  profiles:
    mac_insightface: {provider: insightface, model_manifest: insightface-approved.json, device: cpu}
    ...
```

An explicit `active_profile` overrides the platform default. Profiles hold `provider`, `device`,
an absolute provider `python` and a `model_manifest` (relative paths resolve under
`face_detect/config`). To switch, stop face detection, change `face_detection.active_profile` in
`config/local.json`, and start it again. CPU is the qualified path; CUDA, CoreML and MPS are
options without qualified performance claims. The worker starts a bounded inference subprocess
per image, favouring isolation over throughput.

## The People catalog

A library has one People catalog: the database schema `people`, beside the main schema `public`.
People code connects with search path `people, public`, so face tables reference `public.media`
directly and there's no copy of the photo list. The catalog has its own migrations in
`face_detect/storage/catalog/NNN_*.sql`. Face thumbnails are in
`data/people/thumbnails/<detection id>.jpg`.

**Identities** (`face_detect/spaces/identity.py`):

| Identity | Defined by | Used for |
| --- | --- | --- |
| **Space ID** | Recogniser weights, alignment (`landmarks` or `box`), preprocessing, vector size and normalisation | Which vectors may be compared; the only key for vectors |
| **Detector ID** | Detector weights and settings | Provenance of boxes |
| **Backend** | Device, runtime versions, platform, CPU type | Provenance; never part of the space |
| **Policy ID** | Space + thresholds + calibration | How vectors are compared and grouped |

A new recogniser, alignment or preprocessing rule is a new space; another computer is not.

| Table | Holds |
| --- | --- |
| `detector` | Detector identities (with `legacy_model_id`) |
| `face_analyses`, `face_runs` | Scans per photo, keyed by `detector_id` |
| `face_detections` | Faces: numeric `id` (the library's own key, used by decisions, undo history and thumbnail names) and `face_uuid`, which crosses libraries |
| `space` | `space_id`, manifest, state (`active`, `building`, `retired`), `legacy_model_id` |
| `embedding` | Vectors keyed by `(detection_id, space_id)`, with `backend` and `certificate_id` |
| `certificate` | Library checks per space and backend (`library-sample`, `golden-set`, or `origin` for the backend that made the vectors, which is a record, not a pass) |
| `policy` | Matching thresholds and calibration evidence per space |
| `catalog_state` | `active_space_id`, `catalog_uuid`, `schema_version` |
| People, assignments, constraints, blocks, groups, members, quality, events, ignore events | The user's decisions, keyed by face |

- Records from before the single catalog name the old model ID; each space and detector keeps it
  as `legacy_model_id`, so they still resolve. Retired-model vectors stay in a `retired` space
  and are never used for matching.
- Faces found after the migration get random UUIDs. Migrated faces got a version-5 UUID from the
  photo's content hash and stored box, so every copy of a library agrees.
- Collection links to people name the catalog by its `catalog_uuid`, the same on every computer.
- A permanently deleted photo's faces are removed from the catalog.
- Backups classify the `people` schema, its tables and `data/people/`
  (`app/backup/policy.py`, `compatibility.py`).

**Older libraries** still on a per-model schema (`face_native_<token>`) show no People until the
catalog migration runs, for example after restoring an old backup. With the web server stopped
and a fresh backup, run `python -m face_detect.cli.catalog migrate --from face_native_<token>`.
It copies every table in one transaction and checks each against its source, moves collection
links to the catalog UUID, copies face thumbnails, and points the profile's `runtime.json` at
`people`. The old schema is left unchanged. Rolling back means deploying the earlier release,
setting `runtime.json` `schema` back to `previous_schema`, and restoring the collection links
listed in the migration report; anything edited in `people` afterwards is lost, so roll back
before editing or not at all.

## Moving to another computer

A job pins a space, not a computer, so a paused scan carries on with any backend of the same
space, but a backend writes vectors only after it has passed the **library check** for that
space.

- `face_detect/spaces/library_check.py` uses the library itself as the reference. If the stored
  vectors are from another space it refuses without opening a photo. Otherwise it samples 24 good
  stored faces (one per photo, in a fixed hash order), finds each again at its stored box, embeds
  it and compares it with the stored vector. It passes when every face is at least 0.95, the
  median at least 0.99, and no more than 10% aren't found. Unreadable photos are counted
  separately (`photos-unreadable`, for example an unmounted disk). Only similarity numbers are
  reported.
- The first Discover people on a computer runs the check itself (read-only, about a minute),
  shows the result on the People page and stores the certificate; nothing is written if it fails.
  A library with no stored vectors needs no check.
- A fresh install creates `people`. An install on a library that already has one (for example a
  Windows library restored on a Mac) adopts it, with its people and policy, if this computer's
  model is the same space.
- The catalog's matching policy applies to a backend once it has a passing certificate; until
  then that backend is review-only. A later failing certificate withdraws it again. Measured
  Windows-to-Mac agreement on the CPU backend is about 0.999, well within the policy's ambiguity
  margin.

```sh
python -m face_detect.cli.conformance describe
python -m face_detect.cli.conformance check-library --out certificate.json [--record]
# Measure a new computer before the library moves:
python -m face_detect.cli.conformance check-library --export-sample NEW_DIR                     # old computer
python -m face_detect.cli.conformance check-library --sample-from DIR --out certificate.json   # new computer
# Optional golden set for hardware testing (never family photos):
python -m face_detect.cli.conformance make --images DIR --out NEW_DIR --source "origin and licence"
python -m face_detect.cli.conformance check --golden DIR --out certificate.json
```

A carried sample holds the sampled faces' boxes and vectors, the stored model manifest and a copy
of each sampled photo named by checksum, with no names, IDs or paths. Each photo is checked against
the catalog checksum when copied and when opened. Keep it private and delete it on both computers
afterwards. The golden-set tolerance (0.999) is unmeasured across platforms and stricter than the
library check, so the move doesn't rely on it. `FaceService.embed_geometry(image, geometry)` makes
vectors from stored landmarks or boxes without detection.

## Matching policy and calibration

A fresh install is **review-only**: manual naming and review work, but nothing is attached
automatically until a policy is calibrated for that space. Policies live in the catalog's
`policy` table with their evidence, separate from model assets, so activating one never relabels
models, recomputes vectors or loses decisions. Quality eligibility combines geometry checks with
the policy's detector-score floor.

```sh
python -m face_detect.cli.calibrate                 # read-only evaluation of confirmed labels
python -m face_detect.cli.calibrate --activate      # activate only a qualified policy
python -m face_detect.cli.calibrate --activate --organize-existing   # also an undoable organisation pass
```

- Calibration uses only user-confirmed and manual seed labels for the active space, excluding
  ignored, rejected, blurry, deleted and hidden media. Tuning and held-out evaluation are split
  by capture date (unknown dates excluded); tuning uses a deterministic, identity- and
  date-balanced sample of up to 150 labels chosen without looking at similarity.
- It tests named matching, bounded cross-date grouping (batches of 100), and rejection when all
  references for the right person are withheld.
- **Gate:** zero observed wrong named matches, mixed-person groups and withheld-identity false
  matches, plus at least ten accepted held-out faces across three people. This is a local
  operational gate on saved labels, not a statistical accuracy guarantee.
- User-confirmed equivalent groups (for example the same person at different ages) can share an
  evaluation identity without merging app groups: `scripts/calibrate_native_live.py
  --equivalent-person-ids <id> <id> ...` records them in a private, space-specific
  `identity-equivalences.json`. Supply the full group each time; partial overlaps are refused.
  Never infer an equivalence from names or similarity.
- Evaluation reports and labelled vectors stay in the profile's private `calibration/` directory.
  Each run gets its own directory; `latest-evaluation.json` points at the latest. Activation
  records the previous policy for rollback in `activation.json`. Activation never starts or
  resumes scans or reorganises groups by itself.
- With a policy, **Discover people** attaches confident matches to named people and **Auto-organize**
  uses calibrated named matching plus complete-link merging of up to 500 automatic singleton
  groups per run, preserving manual assignments, exclusions, same-photo restrictions and
  separation constraints, with undo. Existing multi-face groups are never split. **Detect again**
  uses a relaxed 0.50 similarity and two independent anchor dates, and only makes suggestions
  that need confirmation.

## Discover people

- Started per completed import in Imports (Start / Pause / Resume). Opening People never starts a
  scan, and a completed job can't be restarted. Resume uses durable per-photo receipts
  (`familyfoto-face-job-v1`, with verified provider evidence) and keeps decisions.
- It is lower-priority background work: it takes maintenance and content leases around one photo,
  releases them at checkpoints, and yields to imports, video work and duplicate scans, continuing
  when they finish. Pause, disk or checksum failures and service faults need a deliberate Resume.
- Each photo has bounded decode and provider work. Catalog identity, Trash and eligibility are
  checked against live data. Named people are shared across imports.
- **Warnings.** A recoverable decode failure (format or frame layout, size limit, truncated data,
  colour-profile conversion) skips only that photo and is recorded with its reason. The import's
  Warnings dialog groups them with file names; older records that saved only a category say so.
- **Detect again** announces manual priority, waits for the current photo, compares cached faces
  of eligible unnamed groups (both To be reviewed and Review later, across all pages) with
  discovered people, then lets discovery continue. It never decodes every photo again.
- The scan worker caches vectors only (64-bit arrays, 256 MiB bound), revalidated against row
  versions on every cohort fetch; eligibility, visibility, assignments and constraints are always
  fresh reads. Receipt ingestion skips unchanged manual assignments and repairs divergent ones in
  batches. Changes to ingestion need the scan worker paused and resumed.

## People pages

All roles can browse People; changes need an administrator (server-enforced).

- **Discovered people:** named people as cover cards with counts and saved drag order. Group view
  supports inline rename. The main page uses the compact `/cards` endpoint.
- **To be reviewed** and **Review later** (deferred, not ignored): unnamed groups, 100 per page by
  default, via `/review-page`, which selects only the requested page in SQL. Select, drag-select,
  select all (current page) and bulk Match, Ignore and Move to Review later.
- **Possible matches:** Detect again suggestions with Confirm match or Not this person.
- **Ignored faces** (100 per page, Restore) and **Blurry faces**. Ignore excludes a face from
  matching; it never deletes a photo.
- **Review people** on an import opens Review faces scoped to it; a linked import missing from the
  scanned-only choices stays selected as Import #N. Opening People directly clears that scope.
- Person pickers follow the saved People order, with people who have no visible faces at the end.
  Bulk matching and group moves share one **Move to person** menu (arrow keys, Escape).
- **Group photos:** click a cover to open a movable, resizable window, paged at 100 faces, with
  selection, album actions, Move to person, Ignore, Move to Review later and Undo of the latest
  correction. Clicking a face shows its original photo; Set as cover changes only the
  representative image.

**Consistency.** Bulk actions apply to the membership shown in the selected cards. A running scan
may add faces or groups; those are left for review and never silently added to the action.
Server-side checks under the People lock never overwrite faces that were moved, ignored or changed:
unchanged groups are processed, changed ones are left and returned with current cards. Progress
streams through the authenticated bridge (waiting for the lock, checking, processing, saving,
refreshing) with five-second heartbeats; completion is reported only after commit. A broken
connection doesn't retry or cancel; refresh and check before submitting again. Moving selected
faces batches its reads and writes in one locked transaction and runs no inference.

## Blurry faces

New detections pass a conservative crop-quality check (`face-crop-sharpness-v1`): crops with a
short edge under 24 decoded pixels, or Laplacian variance under 8 after downsampling to at most
96 pixels, are set aside. They keep their crop, source, reason and measurements but have no active
observation or group, and are excluded from matching, Detect again and Auto-organize. **Blurry
faces** (100 per page) offers **Send to review**, which releases a face into a new unnamed group
using its stored vector. Existing detections are never filtered retroactively; new thresholds need
a new policy version.

## Evidence compaction

Each vector is stored once per face and space in `embedding`. Scan checkpoints and migration
evidence used to repeat it; `face_detect.catalog.evidence.compact_catalog` replaces those copies
with small versioned references (readers reconstruct the original checkpoint, so receipt hashes
stay valid) and moves `native_migration_faces` to `face_reference_evidence`. It is a transaction
body for a controlled offline maintenance operation, not a startup hook: it refuses without a
verified-backup admission callback, locks the affected tables, validates every reconstructed
payload, drops the old table with `RESTRICT`, and records People catalog version 2. Deploy
compatible readers first; rollback means restoring the pre-compaction backup. Payload savings are
not reclaimed disk space until a separate maintenance step. Clearing `public.face_preferences` is a
separate decision and never part of compaction. On-disk job files are not deleted.

## Legacy data

Docker and CompreFace are not used. The literal `compreface/raw-local` still names historical
on-disk data, and read-only catalog access, migration, backup and audit tools keep that path so
old thumbnails, decisions and backups stay discoverable; don't rename it without a data migration.
Existing model identifiers, embeddings, receipts and SQL history are never relabelled as native
results. Old container checkpoints can't be resumed or ingested; already ingested receipts and
decisions are kept. Retired code is in Git history.

## Not built yet

- **Switching to a new space** while the app runs: building it in the background from stored faces
  and landmarks, reading both until a one-transaction cutover, and reviewing only faces the new
  space places differently. Most stored landmarks came from the retired engine, so a new space
  would first need landmarks from the current detector.
- **Input-rule golden sets** pinning canonical decoding (`face_detect/imaging/face_decode.py`)
  across both platforms.
- **People export and import** between libraries (faces, vectors and decisions by `face_uuid`;
  adopt vectors when the space is present and certified, otherwise rebuild them).
- A periodic drift check that re-embeds a sample of stored vectors.

## Diagnostics and tests

`face_detect.cli.doctor` reports counts, dimensions and identity without printing embeddings; an
optional image is read only and nothing is saved. People HTTP and job logs and durable controls
are under the active profile's data folder. Missing dependencies, altered weights or unavailable
devices fail the chosen provider without fallback. Run `python -m pytest -q face_detect/tests`;
`face_detect/testing/` holds an in-process synthetic provider so People tests need no model.
