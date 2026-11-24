# Mac installer: release build and clean-Mac qualification

The Mac installer is released only after this checklist passes on disposable Apple Silicon VMs.
Never run it on a Mac that holds a live library or backup. `qualify.sh` (beside this file) makes
the automatic checks; everything marked **(manual)** needs a person at the VM.

## 1. Build the release (on an Apple Silicon build Mac)

1. On an Apple Silicon build Mac running macOS 27: `brew upgrade postgresql@17 ffmpeg` and their libraries, so every bundled library matches the
   source Homebrew describes. The audit refuses a payload whose installed version is behind its
   formula (`no matching source for …`).
2. Application: `python installer/release.py build --output out/` from a clean tagged checkout.
3. Native payloads (`installer/macos/build_payloads.py`):
   `postgres --output postgres.zip` and `ffmpeg --output ffmpeg.zip`. Each relocates Homebrew's build
   into one folder, rewrites library references to `@loader_path`, signs ad hoc and refuses anything
   that still points outside. `payload-info.json` inside lists every formula's version, license
   files and source URL.
4. Python and setup window (`installer/macos/build_launcher.py`):
   `python-runtime --archive cpython-3.12.*-aarch64-apple-darwin-install_only.tar.gz --output python.zip`
   (check the archive against python-build-standalone's `SHA256SUMS` first) and
   `setup-window --output setup-window.zip`.
5. Wheels are not built or shipped: setup installs every lock from PyPI by hash. Before
   publishing, check every lock still resolves there: `pip download --isolated --index-url
   https://pypi.org/simple --require-hashes --only-binary=:all: --no-deps -d check -r
   installer/locks/<role>-macos-arm64.txt` for core, password, people-view, insightface and facenet.
6. Models: `buffalo_l.zip` and `20180402-114759-vggface2.pt`, checked against
   `face_detect/config/research-models.json`.
7. `python installer/macos/build_release.py assemble --recipe recipe.json --output release/`
   (recipe format in the module docstring), then `audit --release release/`. The agreement
   shown before anything is downloaded is `installer/legal/agreement.txt` unless the recipe names
   another; check it still matches what the release bundles. Fix every finding;
   nothing is published while the audit fails. Set the recipe's `minimum_os` to the build Mac's
   macOS version (for example `27.2`): the audit names any bundled program that needs more.
8. `build_launcher.py engine --manifest release/manifest.json --payload-root release/payload --output engine.zip`.
9. Online app: `build_release.py descriptor --release release --engine engine.zip --mode online
   --output online.json`, then `build_launcher.py app --descriptor online.json --output "FamilyFoto Setup.app"`
   and `dmg --app … --output FamilyFotoSetup.dmg`.
10. Offline app: `descriptor --mode offline`, `build_release.py bundle --release release --engine
    engine.zip --output bundle/` (audits first), `app --descriptor offline.json --bundle bundle/`, `dmg`.
11. Corresponding source (the agreement promises it for every copyleft program): collect a
    `sources/` folder and name it in the recipe (`"sources"`). The audit refuses the release until
    it holds:
    - for every Homebrew formula in both payloads' `payload-info.json`: its `source_url` archive
      (checked against `source_sha256`), and its recipe and patches exported at the recorded
      `tap_git_head` (`ruby_source_path`) as `sources/homebrew/<formula>.rb`;
    Publish everything in `sources/` as release assets beside the DMG, not as links.
    `payload-info.json` also records the Homebrew version, each formula's revision and its bottle
    checksums. Build releases on a dedicated, clean build Mac (no extra taps or formulas) so they
    can be rebuilt.
12. Publish on the public release repository named by `RELEASE_BASE` (immutable releases on):
    every file in `release/assets.json` under its asset name, `engine.zip`, the online
    `FamilyFotoSetup.dmg`, the sources, and a `SHA256SUMS` file. Check every asset downloads without
    signing in. v0.1.0-beta releases are online-only (see "Installation guidelines" in
    installer/README.md): the offline DMG from step 10 is built only to test in the VMs, not published.

## 2. Prepare the VMs

- FamilyFoto supports **Apple Silicon with macOS 27 or newer** only; older macOS is not supported
  or tested. Use one clean macOS 27 VM per scenario (for example with UTM or Tart). Create a standard
  user, finish Setup Assistant, take a snapshot. Do not install Homebrew, Command Line Tools or Python.
- Tart's `ghcr.io/cirruslabs/macos-golden-gate-vanilla:latest` image is macOS 27.2 without Homebrew or
  Command Line Tools, but in November 2026 it shipped with **Gatekeeper off**. In a disposable clone,
  turn it on (`sudo spctl --global-enable`) so the first launch shows the Open Anyway step users see.
- Run commands in a VM over SSH with a key (`ssh-copy-id`, typing the image's password yourself), or
  share this folder read-only with `tart run <vm> --dir=kit:<path>:ro` (`/Volumes/My Shared Files/kit`).
  A guest may keep showing an old copy of a shared file after it changes on the host; copy updated
  scripts in over SSH (`ssh … 'cat > ~/qualify.sh' < qualify.sh`).
- In each VM: `bash qualify.sh clean`, which must pass (Apple Silicon, macOS 27+, no Homebrew, no
  Command Line Tools, no Python, regular user, Gatekeeper on).
- **Disk size:** the Tart image's macOS container has about 8 GB free, below the 10 GB the setup
  window requires. With the VM stopped, `tart set <vm> --disk-size 100`; after booting, the container
  grows (`sudo diskutil repairDisk disk0` if it has not). Do this once on the snapshot you clone from.
- **Gatekeeper holds unapproved apps:** on macOS 27 a downloaded, unnotarized app stays frozen at
  launch (`_dyld_start`) until someone clicks Open Anyway, even if its quarantine and provenance
  attributes are removed, and turning Gatekeeper off also needs a click. So use two VMs per release:
  - **VM A (automatic):** `bash vm-auto-test.sh FamilyFotoSetup-<id>.dmg < test-admin-password.txt`
    over SSH, with the DMG in the shared folder. It covers the clean check, the quarantined download,
    Gatekeeper's rejection, the bootstrap files (from the app's bundle, checked against its
    `bootstrap.json`, when the launcher is held), the engine with People, an unattended install from
    a fresh shell, start/stop/start, `qualify.sh installed` and an offline install with the network
    blocked. Its working folders are in the home folder: `/var` is a symlink and the engine refuses
    linked paths.
  - **VM B (by hand):** scenarios 1, 3 and 4 below, the real first launch and setup window.
  - **Scenario 8 (restore a Mac backup):** after `vm-auto-test.sh`, VM A makes the backup with
    `vm-make-backup.py` (beside this file): it switches the administrator to a short password you
    can type in a VM window, adds three JPEGs, a HEIC and an MP4 through the app, and makes a real
    verified backup through the backup API onto a disk image (`hdiutil create -size 3g -fs APFS
    -volname FFBackup`). Run it with the installation's Python, the install password and the short
    one on stdin. Move the disk image to a fresh VM B (`hdiutil convert -format UDZO` to copy it,
    `UDRW` to use it), mount it, and restore it there with FamilyFoto Setup by hand.
  - The offline step blocks the internet with `pf` but must leave loopback open (`set skip on lo0`):
    without it, a database connection closing on 127.0.0.1 is left half-open, and a later stop or
    backup refuses ("Stop all database clients").
- Download the DMG **in Safari inside the VM** so it carries the quarantine mark, as a user's would
  (or copy it in and add the mark: `xattr -w com.apple.quarantine "0081;$(printf %x $(date +%s));Safari;" FILE`).
- Restore the snapshot before every scenario below.

## 3. Scenarios

| # | Scenario | Steps | Pass when |
|---|---|---|---|
| 1 | First launch | Open the DMG, drag the app to Applications, open it. **(manual)** Approve once in System Settings → Privacy & Security → Open Anyway. | Exactly one approval; no further prompts for Python, PostgreSQL or FFmpeg. |
| 2 | Launcher check | After scenario 1's approval: `bash qualify.sh app "/Applications/FamilyFoto Setup.app"` | All PASS. |
| 3 | New library, no People | **(manual)** Accept the terms, keep People off, Install; in the setup window create a library. Start FamilyFoto.app. | `bash qualify.sh installed ~/FamilyFoto` all PASS; sign-in works; an import of a few photos and a video (HEIC, MP4) works. |
| 4 | New library with People | As 3 with People on; accept the model terms. | People scan of the fixture photos finds faces; `installed` all PASS. |
| 5 | Agreement and folder refusals | Quit at the agreement; choose a non-empty folder; choose a folder on a nearly full disk. | Nothing downloaded or created each time. |
| 6 | Interrupted download | Cancel during download, open the app again, Install. | Verified downloads reused; setup completes. |
| 7 | Resume after reboot | During restore, restart the VM. **(manual)** In Terminal: `cd ~/FamilyFoto && bash installer/macos/install.sh restore`. | Resumes without Homebrew; `installed` all PASS. |
| 8 | Restore a Mac backup | Make a backup in scenario 3 to a disk image; in a fresh VM open FamilyFoto Setup, choose **Restore from a backup** and that backup, Install and Restore. | Library, accounts and People match; `restore-on-mac/RELEASE.txt` names the release DMG. |
| 9 | Restore a Windows backup | As 8, with a backup made by a Windows release installation. | As 8; People catalog matches (same `buffalo_l`). |
| 10 | Offline | Disconnect networking; `bash qualify.sh offline` all PASS; run the offline DMG through scenarios 3 and 4. | No network request needed; `installed` all PASS. |
| 11 | People not downloaded | Install without People, then try Install People in the setup window. | The switch is disabled with "People was not downloaded"; nothing breaks. |
| 12 | Backup from a release install | In scenario 3, make a Standard and a Fast backup. | Both finish; each has `restore-on-mac/` with `RELEASE.txt`. |

Record for each VM: macOS version, release ID and DMG SHA-256, scenario results, and the
`qualify.sh` output. A failure blocks the release until fixed and rerun from a fresh snapshot.

## 4. Results

| Date | Release | VM | Result |
|---|---|---|---|
| 2026-11-22 | develop 75d48b4, offline DMG, `minimum_os` 27.2 | macOS 27.2, VM A (automatic) | All steps passed: engine with People 3 s, unattended install 20 s, stop 1 s, `installed` 12/12, offline install. |
| 2026-11-22 | same | macOS 27.2, VM B (by hand) | One Open Anyway approval; launcher, agreement, setup window, new library with People, sign-in; `installed` 12/12. Found: the launcher did not check the setup window's 10 GB free space before downloading, and the People text said the model is downloaded (both fixed). |
| 2026-11-23 | main c6441b3, offline DMG with the real terms, `minimum_os` 27.2 | macOS 27.2, VM A (automatic) | All steps passed: engine with People 3 s, unattended install 19 s, stop 1 s, `installed` 12/12, offline install. A small library and a verified backup (5 files, 11 s) with `RELEASE.txt` and no Terminal launcher. |
| 2026-11-23 | same | macOS 27.2, VM B (by hand), scenario 8 | One Open Anyway approval; FamilyFoto Setup, **Restore from a backup**, Copy, with People. Catalog, 5 files, switch-on, thumbnails and move-in passed, then People setup stopped: the library had People on but no faces yet, and the library check raised (fixed: it is skipped with `no-stored-faces`). Resumed with the fix: running with the restored library, `installed` 12/12, the backup's account signs in, 5 media. |
