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
Automatic `Tests` and `Release CodeQL` runs are limited to pushes to `release/**` and
`beta-release-*`, including branch creation and subsequent release fixes. Ordinary PRs,
`main` pushes and `v*` tags do not start either workflow. A newer release-branch push
cancels that branch's older run of the same workflow. Main's protection must not require
CI statuses that intentionally do not run for ordinary PRs; keep its other protections.
Report local tests and obtain review before merging ordinary changes.

| Job | Runs when | Isolation |
| --- | --- | --- |
| Windows safety | Every run | Its own Windows VM; existing platform coverage |
| JavaScript | Every run | Independent Node job |
| macOS core, two shards | Every run | No PostgreSQL; separate VMs |
| macOS database, two shards | Related backend, schema, configuration, installer, script or database-test changes | A fresh PostgreSQL cluster and config per VM |
| macOS Swift | Mac/shared installer, backup/recovery protocol or native test changes | Separate compilation workspace |
| macOS APFS | Backup, storage, recovery, Mac/shared installer or remount-test changes | Disposable disk images on a separate VM |

Release pushes run every test group. The job-selection rules above apply only to an
explicit manual or reusable run with `full` disabled. In that mode, dependency, CI
configuration and shared test-helper changes enable all expensive groups.
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

Cut a release branch from the current main to run full tests and CodeQL before publishing.
Alternatively run **Actions → Tests → Run workflow**, leaving **full** selected, and run
**Release CodeQL** manually on the same commit. CodeQL scans Actions, C#, JavaScript/TypeScript
and Python. There is no weekly scan. Pushing a `v*` tag does not run qualification.
Release build workflows
can call `.github/workflows/tests.yml` as a reusable workflow (full defaults to true) and make
packaging depend on its success. Confirm both tests and CodeQL succeeded for the exact
release commit before publication; these workflows do not themselves block publication.

For diagnostics, deselecting **full** on a manual run compares the selected commit with its
parent. Reusable callers can also select partial qualification explicitly. The change
planner accounts for both sides of renames; missing Git history falls back to all groups.
Labels and commit messages do not select or bypass test groups.

### Release-only policy rollout

1. Disable the existing Tests workflow and GitHub-managed CodeQL default setup while
   changing the policy. Disabled workflows cannot be dispatched manually either.
2. With owner approval, remove only main's required CI status rule so this policy PR
   and ordinary PRs can merge without paid CI. Preserve all other branch protections.
3. Merge the workflow changes, then re-enable Tests. Keep managed CodeQL default setup
   **off**: the checked-in `codeql.yml` replaces it with release-only/manual scans.
4. Update historical release branches to include these workflow changes before pushing
   or qualifying them; workflow definitions come from the selected branch. New release
   branches should be cut from main after the policy merges.
5. Qualification is now deferred until release or explicit manual runs. Review and local
   testing remain necessary; absence of PR checks is not evidence that a change passed.

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
