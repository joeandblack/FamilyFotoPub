# Windows preparation for v0.1.0-beta.2

Status: preparation, not release qualification. No tag, assets, services or server
deployment are created by this document. Companion to release-plan PR #20.

## Identity and handover

- Use release ID and tag `v0.1.0-beta.2`, and project version `0.1.0b2`.
- Build both platforms from the same clean tagged commit. Record the resolved
  40-character commit, manifest digest and every asset's SHA-256.
- Source backups contain a Windows launcher stamped by the same `source_commit`
  check as the Mac launcher. Dirty or unidentified code gets a disabled launcher.
  The launcher fetches only that commit and checks detached HEAD before copying.
- Release backups use the matching official `FamilyFotoSetup.exe` link instead;
  retries remove a stale source launcher. Include `mac_handover` for the matching
  DMG. The Mac-side `windows_handover` manifest/receipt change belongs to the Mac PR.
- Never replace published assets. Fixes require a new release ID and tag.

## Build inputs and gates

The Windows build primitives exist in `build_shared.py`; this is not evidence
that a complete, distributable beta.2 payload set has been assembled or tested.

1. Collect the pinned private Python, PostgreSQL installer and binary archive,
   FFmpeg/ffprobe, application archive, hash-locked core/password/People-view
   wheel closures, and the optional provider closures and model identities.
2. Inventory exact shipped binaries, notices and corresponding source materials
   for FFmpeg and its enabled libraries, psycopg and any other applicable bundled
   components. Do not substitute unrelated upstream source versions. The current
   Windows payload audit checks bytes and license IDs, not source completeness;
   release review must check that separately before publication.
3. Use `installer/legal/agreement.txt` as the common base agreement, with the
   actual release's component notices/inventory available. The Windows dialog
   accepts an agreement file and records its digest. Do not treat one checkbox
   as replacing component terms or as permission to redistribute model weights.
4. Prepare the recipe and bootstrap descriptors. Run the existing `manifest`,
   `audit`, `engine` and `exe` commands and shared offline bundle assembly as
   documented in `installer/common/README.md`. All code inputs must come from
   the tagged checkout, not a server folder.
5. Supply both online and offline descriptors and verify that the offline bundle
   contains every selected payload, including Python/engine and agreement files.
   Keep build/audit logs and checksum lists beside the artifacts.

## Qualification on a disposable Windows VM

Use a fresh full Windows VM with its own virtual media disk and local NTFS install
folder. Windows Sandbox can smoke-test UI/bootstrap only: it did not expose the
disk/partition identity required for real library enrollment in prior tests
(`TEST_RESULTS.md`). Never bypass disk identity checks to make a test pass.

| Scenario | Required evidence |
| --- | --- |
| Fresh Core installation | Login, import, thumbnails/playback, private DB, restart and guest reboot |
| Optional People | Accepted applicable terms, provider installation and actual face processing |
| Windows release backup | Full verification; both platform handovers name beta.2 |
| Windows-to-Windows restore | Fresh destination, catalog/media/People checks, permissions and restart |
| Mac-to-Windows restore | Mac agent's disposable fixture; password, paths, original media, thumbnails and People |
| Windows-to-Mac restore | Supply disposable Windows fixture and checksums; Mac agent reports restore results |
| Empty People backup | People enabled but no faces; restore does not require nonexistent People data |
| Source launcher | Stamped commit, not branch tip; absent/unavailable commit fails without fallback |
| Offline installation/restore | Network disabled; no hidden dependency download |
| Published online installation/restore | Repeat fresh install and restore against final public URLs |

Windows restore already uses the shared `app.migration.move_in` engine and has
Windows-specific access repair. A Mac backup is an intended supported input;
this does not qualify a particular beta.2 Mac fixture without an actual run.

Exchange only synthetic/disposable backups in an owner-approved folder, never
live media, credentials or catalogs. Record commit, artifact digests, OS, scenario,
pass/fail/skip, logs and defects in `TEST_RESULTS.md`. A skip is not a pass.

## Ownership and publication

Windows prepares source handover, EXE/payloads and Windows qualification. Mac owns
its builder/DMG, cross-platform release receipt addition and Mac qualification.
Coordinate the version bump once; freeze only after both prerequisite PRs merge.
Publishing, draft removal, host feature changes and reboots need owner approval.
No hosted CI is required for this preparation PR; local results are recorded.

## Preparation validation (2026-11-23)

- Windows launcher, Windows handover and Mac shared-installer regressions:
  **59 passed, 5 skipped**. Includes a real local Git fetch of an older commit
  while the repository's branch points to a newer commit; no network used.
- Backup capture retry and shared-installer regressions: **152 passed, 2 skipped**.
- Windows PowerShell and hard-link fixtures were run outside the restricted
  agent shell after its authorization restrictions blocked the initial run.
- `git diff --check` passed. No hosted CI, release build, VM installation,
  backup/restore run, deployment or service changes were performed.
