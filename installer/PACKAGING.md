# Release packaging and installed-artifact checks

This is the first packaging/versioning baseline. It preserves the application
directory layout; it does not rename `app`, change database schemas, deploy code,
or resume workers. The version is `0.1.0b1` (tag `v0.1.0-beta.1`), a first public beta, not a qualified stable
release or an assertion about versions of previous unversioned installations.

## Supported artifact

The distribution is a ZIP containing a complete `FamilyFoto/` folder. Rename or
relocate that folder before using the existing Windows/macOS installer. Directory
names and drive letters are not release identities. Modules, web assets, SQL,
provider manifests/locks, platform installers, launch scripts and docs travel together.

`pyproject.toml` records release metadata, Python compatibility, runtime/test
dependencies, core schema compatibility and launch targets. It deliberately does
not declare a wheel backend yet. **Do not use `pip install .` or editable installs
as the product installation procedure.** Wheels require further separation of
writable paths from packaged resources. The ZIP is the supported installer input.

Core installers use `requirements.txt`, now without pytest. Developers/CI use
`requirements-dev.txt`. The builder checks that runtime requirements and project
metadata agree. Provider requirements remain separately locked. Complete transitive
Core locks, SBOM, signing and offline prerequisites remain future release work.

## Build and inspect

From a clean Git checkout, using Python 3.12:

```text
python -m installer.release build --output temp/release-candidate
```

Git is required on the build machine, not the end user's installation. The command
writes a versioned ZIP and adjacent SHA-256 checksum without replacing an existing
archive. Tracked changes are refused unless `--allow-dirty` is supplied for local
tests; that flag is recorded in the manifest. New files must be staged first.
Untracked files are never included.

`installer/release-files.json` is the common allowlist. Only tracked, allowed files
enter the payload. Local configuration, media, database contents, logs, downloaded
runtimes, certificates, tests and synthetic provider fixtures are excluded.
Current top-level operational scripts are included because application messages
and shipped documentation reference them; archived one-time cutover scripts remain
excluded. Inclusion never executes a maintenance command or authorizes its use.
Symlink/junction inputs are refused. Review allowlist changes; it is not a credential
scanner. The Sandbox preparation script uses this builder too, with optional
`-Python` and test-only `-AllowDirty` switches. It does not install on the host.

The manifest records the source commit, dirty flag, application/core schema versions,
and each payload file's size and SHA-256. Validate without opening a database:

```text
python -m installer.release verify "path/to/extracted/FamilyFoto"
python -m app.release
python -m app.release --schema-version 36
```

Checksums detect corruption, not publisher authenticity. Obtain artifacts/checksums
from a trusted release channel. Build release candidates from committed code, test
the resulting artifact, then publish that same artifact without rebuilding it.
Never publish an archive marked dirty as a release. Both ZIP and checksum use
exclusive creation. A handled build failure removes only outputs created by that
invocation so a retry can proceed; abrupt process/host termination can still leave
partial files requiring operator inspection.

## Version and schema policy

- Bump `project.version` for every published release; use prerelease/development
  versions for unqualified candidates.
- Python 3.12 is the currently tested release target, not a permanent ban on newer
  versions. Expand it after both platform/dependency/provider combinations qualify.
- Older partial source updates without `pyproject.toml` display API version
  `unknown` rather than failing startup solely for presentation. Build/verification
  and schema reporting still require valid release metadata.
- Preserve ordered SQL history under `databases/`. Update `core-schema-version`
  with it; mismatched metadata/history fails the build.
- The offline compatibility report requires contiguous applied versions and reports
  compatible, upgrade-required, newer-database-refused, or unsupported-upgrade.
  `--schema-version` assumes contiguous history; it does not certify a real database.
- Existing startup schema upgrades and worker guards are unchanged. This does not
  execute upgrades or add an installer upgrade mode.
- Minimum core schema zero allows fresh creation. It does not promise that every
  historical application/data/model combination has a tested upgrade path. Qualify
  fresh installs and the immediately preceding published release before declaring
  a stable version supported.
- People schemas and provider/model identities remain separately validated. Core
  compatibility does not make embeddings interchangeable.
- Binary rollback after an incompatible schema upgrade needs a matching backup
  restore; do not launch older code against a newer database.

## Entry-point inventory

`tool.familyfoto.entrypoints` lists ASGI targets, module commands, and launch scripts.
The artifact test imports modules without executing main functions, resolves ASGI
targets, and confirms scripts exist. An AST test detects literal Python `-m app.*`
and `-m face_detect.*` calls missing from the inventory. Dynamic strings and shell
commands still require review. Real decoder commands run on synthetic JPEG bytes.

## Safe artifact test

```text
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_release_packaging.py -q
```

The test builds once, extracts outside the repository under a path with spaces,
and launches an isolated interpreter from an unrelated working directory. It checks
application imports originate in the artifact, validates resources/schema metadata,
exercises HTTP login/static/auth-gate routes, and detects corrupted files.

The HTTP smoke process uses monkeypatches and Python audit hooks as accident guards,
not an OS security sandbox. It rejects audited connections, bind/DNS/sendto/sendmsg,
subprocesses, and writes/deletes/renames outside the extracted folder. Native code
or unaudited operations are not comprehensively contained; only trusted application
code belongs in this test. Production lifespan is never run. Windows' event-loop wakeup
socketpair is created before installing that guard. Set
`FAMILYFOTO_PACKAGE_TEST_ROOT` to an existing disposable directory on another drive
to exercise that path. The harness creates/removes only its own temporary child.
The standalone smoke script refuses existing configuration/state, then creates
disposable configuration/logs inside the extracted copy; never run it on a real install.

Both Windows and macOS CI jobs run these tests. Third-party dependencies come from
the test interpreter; application code/assets come from the artifact only. This is
**not** a fresh prerequisite install, database integration, model inference, VM
reboot, or upgrade qualification. Those remain separate release gates.
