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

GitHub's hosted macOS and Windows runners are billed (macOS at ten times the normal rate), so CI
runs once per batch of work rather than on every push.

1. Feature branches start from `develop` and open pull requests **against `develop`**. These run
   no GitHub tests.
2. **macOS:** before pushing, run `ruff check .` (configured in `pyproject.toml`; it must report
   nothing) and the full suite locally against a throwaway PostgreSQL
   (`FAMILYFOTO_INTEGRATION=1 python -m pytest`, plus `node --test tests/*.test.mjs`; the
   packaging tests need a real git checkout). The pull request lists the commit tested, the
   counts, and anything that couldn't be verified.
3. **Windows:** the Windows reviewer fetches the pull request, runs the Windows checks, and
   comments the result with the commit it tested.
4. Merge into `develop` only when both results are for the pull request's **latest** commit.
   Any later push means re-running the platform that is now out of date.
5. When a batch is ready, open one pull request `develop` → `main`. Only that one runs CI
   (Windows `safety` and macOS with a disposable PostgreSQL), which is also the first test of the
   changes together on a clean machine. It catches problems local runs miss, such as a long
   hostname that once broke certificate creation.

A newer push cancels that pull request's still-running CI (`concurrency` in
`.github/workflows/tests.yml`).
