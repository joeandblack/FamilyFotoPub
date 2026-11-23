# Development

How the code is laid out, how to run it from a checkout, how to test it, and how changes land.

Contents: [Layout](#layout) · [Running from a checkout](#running-from-a-checkout) ·
[Tests](#tests) · [Branching and CI](#branching-and-ci)

## Layout

```text
app/                 Python API, workers, backup and domain logic
web/                 Browser interface (vanilla HTML and ES modules)
face_detect/         The People package (see face_detect/README.md)
fastbackup/          The Fast backup engine (see fastbackup/WHITE_PAPER.md)
databases/           Versioned PostgreSQL schema SQL
installer/           Windows and Mac installers and the shared installer engine
scripts/             Operator scripts (see scripts/README.md)
tests/               Python, JavaScript and browser tests
docs/                This documentation
config/example.json  Public template; config/local.json is private and ignored
data/ logs/ temp/ runtime/ .venv/   Local state, never committed
```

Source, tests, migration SQL, documentation and workflows belong in Git. Never commit databases,
media, configuration credentials, TLS material, models, logs or temporary files.

## Running from a checkout

For normal installs use [`installer/`](../installer/README.md). To run from a source checkout on
Windows (Python 3.12+, PostgreSQL 17, a local destination disk):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe installer\configure.py
.\.venv\Scripts\python.exe installer\init-database.py
# Administrator PowerShell: Restart-Service FamilyFotoPostgres
.\.venv\Scripts\python.exe -m app.database
.\.venv\Scripts\python.exe -m app.service.server
```

`requirements.txt` holds the runtime dependencies and `requirements-dev.txt` adds pytest and
ruff. Review the enrolled disk serials in `config/local.json` before importing, and never copy
another installation's machine configuration. See [Operations](OPERATIONS.md#database) for
PostgreSQL and [Operations](OPERATIONS.md#https) for the development HTTP switch.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
$env:FAMILYFOTO_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/*.test.mjs
ruff check .
```

- Integration tests (`FAMILYFOTO_INTEGRATION=1`) use generated fixtures under `temp/tests` and a
  disposable, uniquely named PostgreSQL schema. They never touch real media disks or the live
  library.
- `FAMILYFOTO_BROWSER=1` adds tests that drive an installed Chrome (for example the upload and
  download workflows in `tests/test_uploads.py` and `tests/test_downloads.py`). Standalone
  browser checks include `tests/browser-smoke.py`, `tests/import-options-browser.mjs`,
  `tests/selection-browser.mjs`, `tests/viewer-caption-browser.mjs`,
  `tests/photo-zoom-browser.mjs` and `tests/login-browser.mjs`. All use synthetic or isolated
  data, and none installs a browser.
- `face_detect/tests` hold the People package tests; run them too.
- Disposable test databases are recreated when their schema is behind; never migrate a stale
  throwaway database or bypass the verified-backup requirement. For a development catalog with
  data you need to keep, follow [Backup](BACKUP.md#schema-upgrades).
- Keep generated evidence, local configuration, catalog backups and run histories outside Git.

## Branching and CI

Use `feature/`, `fix/`, or `doc/` branches and open PRs against `main` in FamilyFotoPub.
The `Tests` workflow runs on PRs into `main` and pushes to `main`. A newer push cancels
that PR's older run. Windows `safety` and the aggregate `macos` result remain required.

| Job | Runs when | Isolation |
| --- | --- | --- |
| Windows safety | Every run | Its own Windows VM; existing platform coverage |
| JavaScript | Every run | Independent Node job |
| macOS core, two shards | Every run | No PostgreSQL; separate VMs |
| macOS database, two shards | Related backend, schema, configuration, installer, script or database-test changes | A fresh PostgreSQL cluster and config per VM |
| macOS Swift | Mac/shared installer, backup/recovery protocol or native test changes | Separate compilation workspace |
| macOS APFS | Backup, storage, recovery, Mac/shared installer or remount-test changes | Disposable disk images on a separate VM |

Dependency, CI configuration and shared test-helper changes enable all expensive groups.
Documentation-only and web-only changes skip the database, Swift and APFS groups; ordinary
Python and JavaScript tests still run. Consequently, database-backed browser integration
coverage for web-only changes is deferred to full qualification. Backend changes deliberately
trigger database tests even when no SQL file changes: API queries, authentication, uploads,
People and backups all depend on the database contract.

`scripts/ci_tests.py` owns the change rules and pytest partition. Database classification
follows imports of shared test fixtures; an entire test module stays on one shard. Explicit
`ci_swift`, `ci_apfs` and `ci_database` markers isolate native tests from mixed modules.
Every collected test belongs to exactly one group. Normal local pytest ignores this partition
unless `-p scripts.ci_tests --ci-group ...` is supplied. New native tests must carry the matching
marker. Platform/hardware/explicit-opt-in skips remain in force; full qualification does not
supply a real media disk or an externally managed backup test cluster.

The `macos` summary runs even when dependencies fail. It requires ordinary jobs to pass,
every scheduled expensive group to pass, and disabled groups to be skipped. A failed or
cancelled scheduled group cannot be reported as a successful bypass.

### Release qualification

Run **Actions → Tests → Run workflow**, leaving **full** selected, before publishing a release.
Pushing a `v*` tag also runs all groups, regardless of changed paths. Release build workflows
can call `.github/workflows/tests.yml` as a reusable workflow (full defaults to true) and make
packaging depend on its success. A tag-triggered run alone is not a pre-publication gate.

For diagnostics, deselecting **full** on a manual run compares the selected commit with its
parent. Ordinary PRs compare their complete branch diff with the merge base, including both
sides of renames; missing Git history falls back to all groups. No labels or commit messages
bypass the required checks.

```sh
# Inspect the change plan or collect every test's group without running fixtures.
python -m scripts.ci_tests --base origin/main --head HEAD
python -m pytest -p scripts.ci_tests --collect-only --ci-report=temp/ci-groups.json
# Run one ordinary shard; omit shard options to run the whole group.
python -m pytest -p scripts.ci_tests --ci-group core --ci-shards 2 --ci-shard 0 -q
```

CI reduces wall time through independent jobs; full runs may use more total runner minutes.
The PR should report measured timings, test counts and any remaining skips, not an assumed
speedup. Keep local integration runs on throwaway databases and synthetic media only.
