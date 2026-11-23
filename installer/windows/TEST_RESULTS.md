# Windows Core Installer Test - 2026-10-29

## November 14 Independent Python Runtime

The fresh Windows installer now extracts the official PSF Python 3.12.10 x64
NuGet package instead of invoking the globally registered EXE installer. This
avoids maintenance-mode redirection to an older installation of the same version.

- Real-package validation passed all **7 runtime tests**, including provisioning
  two separate runtimes and creating two virtual environments from each.
- Verified package SHA-256, publisher signatures, isolated standard-library
  imports, runtime/venv prefixes, and `pip check` in each empty environment.
- Registered Python keys and the registered executable/DLL hashes were unchanged.
- Failure tests cover existing targets, corrupt archives, invalid publishers,
  failed runtime probes and a target created concurrently before publication.
- Wider installer regression suite: **143 passed, 1 skipped, 1 failed**. The
  skipped test is the opt-in real-package test (run separately above). The failure
  is `test_the_runner_passes_the_password_on_standard_input_and_reads_all_output`:
  subprocess output decoding fails under cp1252. It was reproduced separately
  on unchanged release base `e0c7565`; this PR does not alter the dialog runner.
- Tests use disposable directories only. No existing installation, configuration,
  service, database or media was changed. Full Core/People dependency installation,
  model inference, end-to-end restore and reboot were not repeated in this test.

Opt into the real-package test by setting `FAMILYFOTO_TEST_PYTHON_PACKAGE` to the
downloaded official `python.3.12.10.nupkg`, then running
`python -m pytest tests/test_windows_private_python.py -q`.
The expected archive SHA-256 is
`0EB85C2DFCCCCF1B17352DE4C397F69194035B7D37149EACC16F1147D93DE3B8`.
Use a short disposable `--basetemp` for the wider Windows packaging tests:
deeply nested pytest/worktree paths can exceed PowerShell 5.1/.NET path limits.

## October 31 Mac Return-Handoff Validation

Validated Mac commit `80b89f3` in a detached Windows worktree, without changing
the running application checkout until validation completed. The local branch
was clean and behind by one commit; no reset, stash or discarded changes were needed.

- The requested focused Windows suite passed: **52 tests**.
- With added Windows shared-verifier/path and package-content regressions, the
  wider installer/native suite passed: **115 passed, 19 skipped**. All 19 skips
  are the explicitly macOS-only installer tests, not untested Windows failures.
- The sanitized Windows package includes `installer/native_people.py` unchanged
  (SHA-256 compared with source), and excludes private configuration. Its
  `install.ps1 -Mode Plan -WithPeople` resolved the staged package root correctly.
- Updated only the adapter/shared-verifier source in the existing disposable
  `FamilyFoto-native-install-test-1e378986` installation. Started only its private
  PostgreSQL cluster on 55481, ran `people.py --mode verify`, and stopped it cleanly.
  The real Windows provider venv prefix, model/catalog identity, empty catalog,
  review-only policy and disabled service autostart all passed; People port 55483
  was preserved. No scans or People HTTP services were started.
- Added explicit tests accepting a real Windows `Scripts/python.exe` venv and
  rejecting a foreign prefix or an external model manifest before execution.
- Live import #3 remained running and #1 paused. No live configuration, media,
  database, service or scan controls were modified.

This revalidated the shared verifier against an existing **disposable empty**
installation. It did not repeat dependency installation, model inference, a full
fresh VM install, media import or reboot testing. Earlier evidence and remaining
qualification limits below still apply.

## October 31 Fresh Native People Test

A separate directory, fresh application/provider virtual environments, generated
credentials and new PostgreSQL cluster were used. Ports 55481-55483 were reserved
for this test. The existing PostgreSQL executable was reused read-only; cached
wheels and the approved model archive were reverified by the dependency/asset
hash checks. No live configuration, catalog, media, labels or matching policy was
copied. This is not a fresh Windows VM or a fresh online-download test.

- Core dependencies installed, an empty database initialized and all 34 schema
  upgrades applied. InsightFace CPU and application math dependencies installed;
  both environments passed `pip check`.
- An empty native model-scoped catalog was created with custom People port 55483.
  Service autostart remained off, matching remained review-only and no jobs started.
- Initial final verification failed because the new verifier used `faces` instead
  of the actual `face_detections` table. After correcting the verifier, read-only
  verification passed against the same fresh catalog. The initial failure is
  retained in local test evidence; this was not an uninterrupted all-green run.
- Real inference: generated blank image, zero faces; scikit-image's bundled
  astronaut example, one face, 512-dimensional normalized embedding. Observed
  times were 6.22s and 2.48s, not an accuracy or throughput benchmark.
- 112 focused installer/native/macOS preparation regression tests passed.
- The isolated database was stopped cleanly after verification. Live import #3
  was left running; the paused import #1 was not resumed.

Local evidence (private,
not committed), including `logs/native-install-test.log` and the initial failure
receipt. The subsequent verification/inference results are recorded above.

Still pending: complete master `-WithPeople` clean-VM run, destination enrollment,
real import/service startup from Settings, reboot persistence, and full fresh
online downloads. Installation deliberately does not enroll a destination merely
to start the HTTP People service before the first import.

## October 31 Layout Regression Checks

Windows setup scripts, Sandbox helpers and Windows-only guides now live in
`installer/windows`. Shared Python helpers remain under `installer`; macOS paths
are unchanged. Updated application-root detection, subprocess paths, setup
fingerprints, Sandbox launch commands and documentation links.

- 47 installer/macOS preparation tests passed, including PowerShell parsing,
  read-only Plan, fingerprint resolution, credential handling and sanitized packaging.
- Read-only Plan succeeded in the working checkout and staged package.
- The staged package includes native gateway modules/browser assets required by
  the application even when inference is disabled. Live config, data and runtime
  directories are excluded.
- Host execution policy blocked direct unsigned script execution. Tests used
  process-scoped `-ExecutionPolicy Bypass`; no persistent policy was changed.
- The live import #3 scan remained running; import #1 remained paused.

This is path/package regression validation, not a new full Sandbox installation
or VM reboot test. The older successful run below does not qualify the latest
native-provider deployment. Fresh native People provisioning and full VM media
import/reboot tests remain required.

## Scope

An isolated Windows Sandbox was used on the development host. Only sanitized source
files and verified dependency installers were provided. Live configuration,
database, media, certificates and credentials were not mapped or copied.
A dedicated test-results directory was writable. CompreFace/Docker was not tested.

## Passed

- Automatic private CPython 3.12.10 x64 installation with checksum and publisher checks.
- Virtual environment, pinned direct requirements and `pip check`.
- Unattended guest-only administrator password input; configuration stores its hash.
- PostgreSQL 17.11-5 service, restricted application role and loopback-only binding.
- All 34 database schema upgrades on an empty database.
- FFmpeg/ffprobe 9.0.2 provisioning and version checks.
- Desktop shortcut/task creation and first application launch.
- HTTP login, rejection of unauthenticated Library access, empty Library and Imports,
  album listing/creation, storage settings, and storage deferred until first import.
- Graceful server stop/start, followed by the login page becoming available again.
- Resume from a stopped download checkpoint without repeating completed setup steps.

## Findings and Fixes

- `Get-Volume` did not expose Sandbox C:. The installer's NTFS/free-space check now
  uses `System.IO.DriveInfo`; the installation then proceeded normally.
- The guest PostgreSQL network download stalled with zero bytes written. It was
  stopped only at the download checkpoint, before native installation. A cached
  official installer matching the pinned SHA-256 was supplied and Resume succeeded.
  FFmpeg also used a checksum-verified cache. A fresh online PostgreSQL/FFmpeg
  download remains unqualified in this guest.
- Downloads now suppress PowerShell 5 per-chunk progress overhead and have a
  600-second timeout. FFmpeg uses the shared noninteractive download helper.
- Native Python/MSVC installers were slow in Sandbox, but completed successfully.

## Still Required Before Release

- A full Windows VM test of destination enrollment, real photo/video import,
  thumbnails/playback and disk disconnect safeguards. Sandbox does not expose the
  partition/disk identity required by the application. The smoke test records this
  as skipped, not passed; no disk safety check was bypassed.
- Windows reboot persistence and interactive desktop-launcher/browser testing.
- Fresh online dependency-download qualification, a maintained Python security
  build (the official 3.12.10 Windows installer is only the compatibility test pin),
  transitive dependency locking, licensing notices and signed release packaging.
- Former Docker/container provisioning was not tested and has since been retired;
  use the native People installer instead.

## Reproduction

Run `installer/windows/prepare-sandbox-test.ps1 -RunCoreTest` and open its generated `.wsb`.
The disposable test harness runs setup, HTTP checks and server stop/start checks.
Inspect `status.json`, installer output, `smoke.json` and `restart.json` in the
generated `results` folder. A `null` smoke-check result is a skip, not a pass.

This run's local evidence is under
`temp/installer-sandbox/test-b2daefc8285541fe8a5c486ec4b2a43e/results/`.
`resume.log` contains the successful installation after the initial disk-check
failure and stalled download; retain those earlier failures as diagnostic evidence.
