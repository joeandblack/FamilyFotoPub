# Restore from a backup into a fresh Mac installation — 2026-11-12

Branch `feature/one-stop-restore`. The Windows test backup of 2026-11-11 (318 media: 302 photos,
16 videos, 3,315,962,224 bytes; 776 faces, 7 people), restored with `install.sh restore` into two
fresh code-only copies under `~/TESTSPACE`, each with its own ports, beside the existing test
installation, which kept running.

| | Copy, with People | In place on the exFAT SSD, without People |
|---|---|---|
| Ports (database, web, People) | 5434, 4342, 54343 | 5435, 4343 |
| Backup | the original, left whole | a scratch copy, used up as designed (`MEDIA-MOVED.json`) |
| Restore and check | passed | passed, `--allow-unprotected-media` |
| Switch warning | stopped there unattended without `--old-library-stopped`; carried on with it | `--old-library-stopped` |
| Thumbnails, posters, face pictures | 1,094 restored; repair rebuilt 0, failed 0 | 1,094 restored; repair rebuilt 0, failed 0 |
| Settings | address, port and FFmpeg from this Mac; accounts from the backup | same |
| People | set up; library check passed, 24 of 24, worst 0.9998, median 0.9999998 | not installed; view-only |
| FamilyFoto | running at the end | same |

Found and fixed during the runs: carrying on passed the default People port instead of the
recorded one; and without People, the view-only People layer needed numpy and scipy, which
People setup normally installs, so the restore now installs just those two. Not run: the
interactive prompts (file pickers, typing `switch`), a browser check, a large library. After a
macOS update restarted the Mac, neither came back by itself, as for any Mac install (the service
is on demand); `manage.py start` brought both back.

# Mac fresh native installer validation — 2026-10-31

Base revision: `d56b91f`, with the local Mac installer continuation changes.
Host: Apple Silicon arm64, macOS 27.0.1 (26A434), 16 GiB RAM.
Existing Homebrew tools: Python 3.12.16, PostgreSQL 17.11, FFmpeg 7.1.1_2.

## Post-merge read-only Plan increment

On `codex/macos-installer-qualification`, based on merge `68789a0`, the focused
Plan, preparation, People, lifecycle and Windows adapter tests produced **64 passed,
2 Windows-only skips**. Plan ran against the existing Mac checkout: it found the
required tools, reported default ports available, and correctly identified existing
installation state. No installer or service lifecycle command was executed in this
post-merge check. Earlier integration results below were not rerun by this increment.

## Scope and isolation

Used fresh code-only folders beneath ignored `temp/`, including spaces in their
names. No existing configuration, database, credentials, face profile, calibration,
or media was copied. The second run used ports 55491/55492/55493. New test
passwords were generated and passed through stdin, never arguments or Git.

The full `install --with-people --research-only` workflow ran with new app and
provider environments and a new private PostgreSQL cluster. Existing Homebrew
binaries and pip's wheel cache were reused. The official 275 MiB model archive
was downloaded anew and its pinned checksums verified. This is not a clean-OS
or Homebrew bootstrap test, nor a cold-cache dependency download qualification.

## Results

- Focused installer, lifecycle, provider, calibration and data-root regressions:
  **92 passed, 1 skipped** (Windows-only execution).
- Core configuration, private database initialization, schema upgrades, restricted
  application role, dependencies and local app bundles completed successfully.
- Fresh native InsightFace catalog created; review-only, autostart off, no media
  enrollment and no scans. The private bootstrap database stopped cleanly.
- Read-only People verifier passed against the explicitly started test database.
- Blank image: zero faces. Bundled scikit-image astronaut: one face, normalized
  512-value embedding. Observed doctor times: 10.94s and 1.89s respectively,
  including startup; not a throughput or accuracy benchmark.
- Repeat Core/People installation attempts were refused; config and receipt hashes
  remained unchanged.
- Native launchd startup served login and Mac health endpoints with HTTP 200;
  graceful stop returned successfully. After the TIME_WAIT correction, two
  consecutive immediate launch/login/stop cycles passed and all three test ports
  were available for server rebinding after each cycle.

## Failures retained and corrections

The first fresh run failed. The initial verifier treated normal POSIX venv Python
symlinks as external assets. It now checks the private executable directory and
queries the interpreter's actual virtual-environment prefix. A regression test
accepts a genuine venv and rejects an unrelated interpreter.

The same run exposed pooled application connections keeping PostgreSQL smart
shutdown waiting. Bootstrap now closes its own pools before signaling only its
owned PostgreSQL child. A successful installation receipt is written only after
shutdown completes. The failed test folder and private logs were retained.

The first launch/stop probe found no active listeners but a plain bind still
failed on TCP TIME_WAIT. Port preflight now uses SO_REUSEADDR to match server
rebinding. Active-listener rejection and closed-connection rebinding are both
covered by socket tests.

## Reproduction and remaining work

Use a new code-only directory and the README's custom-port installation command.
Then explicitly run:

```sh
.venv/bin/python3 installer/macos/test-native-people.py --run
```

This harness requires an empty, unenrolled installation; starts/stops only its
private database; runs read-only verification and disposable sample inference;
and records `logs/native-people-smoke.json`. Existing production state is rejected.

Still pending: fresh OS/bootstrap qualification, full cold-cache dependency
fetches, interactive first import and review acceptance with an approved disposable
volume, physical unplug/replug, logout/reboot/sleep, signed/notarized distribution,
and upgrade/recovery workflows. The whole repository suite was not rerun.
No personal media was accessed and the existing Mac installation was not started.
