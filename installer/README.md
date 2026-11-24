# FamilyFoto Installer

See [Packaging](PACKAGING.md) for release metadata, the shared ZIP payload builder,
schema compatibility reporting, and isolated installed-artifact checks.

Platform entry points: [Windows](windows/WINDOWS_INSTALLATION.md) and
[macOS](macos/README.md). Shared configuration, database initialization and
verification helpers remain directly under `installer/`.

Installation-only code and documentation live here. Run from the repository root:

```powershell
powershell.exe -NoProfile -File installer/windows/install.ps1 -Mode Plan
```

Use `-Mode Install` only for a fresh installation in elevated Windows PowerShell
5.1. The application root is the parent of `installer/`, regardless of the shell's
current directory. This folder is not a standalone distribution: ship it with the
application. Place that whole package in the desired local installation folder.
Source and destination media folders are chosen later in Imports.

## Installation guidelines

These apply to both platforms' installers (FamilyFoto Setup.app and FamilyFotoSetup.exe), for a
new library and for a restore from a backup alike.

1. **Online installation.** Setup installs FamilyFoto's own code, then each third-party component
   it needs (Python, PostgreSQL, FFmpeg, Python libraries, and, if chosen, People). For each
   component it first looks in the setup's temp folder and uses the file there if it is present
   and correct; otherwise it downloads it and stores it there, so an interrupted installation
   resumes without downloading again. Today that folder is the per-user setup cache (Mac
   `~/Library/Application Support/FamilyFoto/Setup/<manifest>`, Windows
   `%LOCALAPPDATA%\FamilyFoto\Setup\<manifest>`), plus the installation's `temp` folder for
   Windows FFmpeg. The target is to remove it once setup succeeds (see the known gaps below).
2. **Offline installation.** One file (a zip, dmg or msi/exe) carries FamilyFoto and the
   third-party binaries it needs. Opening it fills the temp folder, then the online installation
   runs as above and finds everything it needs there. Third-party *source code* is not needed to
   install: it is published beside the release for its licences.
3. **Checksums.** Every component, packaged or downloaded, is checked against the SHA-256 that
   the release manifest records for it before it is used. Downloads come only from the pinned
   official URLs; a mismatch stops setup and nothing is used.
4. **Components FamilyFoto cannot redistribute** are downloaded from their official source during
   setup, also during an offline installation, and checked against a pinned SHA-256. Today these
   are FFmpeg on Windows (gyan.dev) and People's Python libraries on both platforms (PyPI).
5. **Optional features** such as People are downloaded only if chosen, and only after their terms
   are accepted.

The shared engine follows 1 to 3 for every component in the release manifest
(`installer/common/acquire.py`: the temp folder first, then an offline bundle, then a download;
offline mode refuses to download). Rule 4's components are fetched by the step that installs
them (`installer/windows/install-ffmpeg.ps1`, `face_detect/cli/setup.py`).

**Current release: online installation only.** v0.1.0-beta releases publish the online
installers (FamilyFotoSetup.dmg, FamilyFotoSetup.exe) and their payloads. The offline DMG and
bundle are built only as internal test vehicles: they run the same installation with the temp
folder already filled. Known gaps, not investigated yet:
- the setup cache is not removed after a successful setup yet (about 0.9 to 1.3 GB stays behind);
- for a later offline release: rule 4 makes an offline installation need the internet for those
  components; People's libraries go straight to PyPI (pip's own cache aside) instead of checking
  the temp folder first; and the Windows offline test vehicle is a folder (the bundle and its
  FamilyFotoSetup.exe), not yet the one file rule 2 describes.

## Contents and Boundaries

- `windows/install.ps1`: master Windows Plan/Install/Resume/Verify entry point.
- `windows/setup-common.ps1`: Windows path checks, ownership, downloads and checkpoints.
- `configure.py`: credentials and initially unconfigured media storage.
- `windows/install-python.ps1`: private Python installation.
- `windows/install-postgres.ps1`, `init-database.py`: Windows installation/shared database bootstrap.
- `windows/install-ffmpeg.ps1`: local Windows FFmpeg/FFprobe binaries.
- `windows/install-desktop-launcher.ps1`: Windows desktop shortcut/task registration.
- `verify-installation.py`: read-only dependency and database verification.
- `upgrade-catalog.py`: shared read-only upgrade plan and explicit offline,
  backup-protected schema application; never starts/stops services or deploys code.
- [Windows guide](windows/WINDOWS_INSTALLATION.md): operating instructions.
- `macos/`: macOS preparation, installation and lifecycle adapters.

## Existing catalog upgrades

Fresh-install scripts do not adopt or update existing installations. For a pending
catalog schema change, run the **new release's** Python and shared helper with the
existing installation's explicit config and data roots:

```text
python installer/upgrade-catalog.py --config-root SOURCE_CONFIG --data-root SOURCE_DATA
```

The default is a read-only plan. It uses a read-only database connection, reports
pending versions and whether backup is required, and creates no application paths.
It does not load settings through normal runtime initialization. Keep credentials
in the private source configuration or PostgreSQL password file, not command arguments.

After stopping the old app and all writers yourself, applying a catalog-only
upgrade requires a new backup directory, the administrator password (which protects it), explicit
`pg_dump`, and a conservative byte budget:

```text
python installer/upgrade-catalog.py --mode apply --offline --catalog-only-upgrade --config-root SOURCE_CONFIG --data-root SOURCE_DATA --backup-directory NEW_BACKUP_DIRECTORY --pg-dump PATH_TO_PG_DUMP --database-budget-bytes CONSERVATIVE_DUMP_SIZE_BYTES
```

Only trusted SQL under this new release's `databases/` is used. This does not deploy
the release, install its dependencies, edit source configuration, or resume scans.
Pending upgrades on existing catalogs refuse ordinary startup without the verified
backup gate. Fresh/current schemas need no backup. Custom connection options and
validation-only recovered configurations are refused rather than silently adapted.
See [recovery commands](../docs/BACKUP.md#command-reference) for failure handling.
This shared entry point has Windows fixture validation; actual macOS upgrade and
release deployment qualification remain outstanding.

Shared runtime assets deliberately remain outside this folder: `requirements.txt`,
`requirements-faces-windows.txt`, `databases/`, `face_detect/config/`, and the desktop
runtime scripts under `scripts/`. Tests remain under `tests/`. Do not duplicate
these assets here or move live launcher targets. Downloaded binaries, credentials
and setup checkpoints stay in ignored runtime/config/data directories, not in Git.

## Software and User Involvement

Current People uses the native Windows provider, not Docker/CompreFace. See
[native provider setup](../docs/PEOPLE.md). Native runtime,
model/license approval and empty-profile provisioning are available through the
optional [Windows People step](windows/README.md#native-people), after Core setup.
Start face service with app stays disabled by default;
installation must not start scans or copy production identities/calibration.
Full VM import/reboot qualification remains pending. The Core coordinator asks
whether to install People and shows the model terms first; unattended installs choose
with `-WithPeople -ResearchOnly` or `-WithoutPeople`. Model approval is explicit.

These are the repository's current pins/targets, not claims of latest releases.
Silent installation means no component wizard, not bypassing authorization.

| Component | Needed for | Silent capability / user involvement | Current master script |
| --- | --- | --- | --- |
| Windows 11 x64, Windows PowerShell 5.1 | Server and setup | Existing OS; administrator approval for setup | Checks prerequisites |
| CPython 3.12 x64, pip, venv | Application | Verified full installer supports `/quiet` | Installs private 3.12.10 runtime; optional explicit `-Python` override; creates venv |
| Python libraries listed below | Core | pip installs noninteractively inside app venv | Installed automatically |
| PostgreSQL 17 (pinned installer 17.11-5) | Catalog database | EDB unattended installer; elevation required, generated DB credentials need no typing | Installed/configured automatically |
| FFmpeg and FFprobe 9.0.2 | Video tools | Verified archive extraction, no wizard | Installed automatically |
| Microsoft Visual C++ runtime where required | Native executables/wheels | PostgreSQL installer supports runtime provisioning; verify on clean Windows | Delegated to native installer; not a separate pinned step |
| Modern web browser | Client UI | Reuse installed browser; no new browser required | Not installed |
| NumPy 2.2.6, SciPy 1.15.3 | People grouping/quality checks | Hash-pinned wheels install noninteractively | Separate People prerequisite |
| InsightFace CPU and ONNX Runtime | Native face detection/recognition | Hash-pinned Windows wheels in an isolated environment | Optional native People step |
| Approved InsightFace model assets | Native embeddings/detection | Explicit research-use approval, pinned archive and asset checksums | Optional native People step |

The former CompreFace container deployment is historical and not needed by the
native installer. FamilyFoto and native CPU People work without Docker, WSL or a
GPU. No Git, Node.js, Codex, pgAdmin,
Stack Builder or compiler toolchain is required to run the packaged app.

### Direct Python Libraries

Core manifest: FastAPI 0.115.12, Uvicorn 0.34.2, psycopg[binary] 3.2.9,
psycopg-pool 3.3.3, Pillow 13.0.0, pillow-heif 1.8.1, HTTPX 0.28.1,
cryptography 50.0.0 (the private HTTPS certificate authority).
pytest 9.0.3 is in `requirements-dev.txt` for tests, not the production manifest.
People adds NumPy/SciPy above. pip resolves supporting dependencies (for example
Starlette, Pydantic and AnyIO); these do not need separate user installation.
A complete transitive lock/SBOM remains a release-packaging task.

### Minimum Interaction Target

Core: choose the application folder by placing the package there, approve elevation,
enter/repeat the FamilyFoto administrator password, then wait for automated steps.
Python is provisioned automatically without adding PATH, shortcuts or a launcher. For People, additionally
approve model terms. No GPU driver installation or reboot is requested by native People setup. Optional LAN
TLS certificate trust/firewall setup also needs consent. No media paths at setup.
Never silently accept third-party terms or reboot the user's computer.

### Vendor References

- [Python unattended installation](https://docs.python.org/3.12/using/windows.html#installing-without-ui)
- [PostgreSQL installer parameters](https://www.enterprisedb.com/docs/supported-open-source/postgresql/installing/command_line_parameters/)
- [Native provider setup and model terms](../face_detect/README.md)

Core installation, HTTP smoke checks and server stop/start passed in Sandbox on
2026-10-29. Full VM storage/import/reboot testing, complete People provisioning and
signed distribution remain unfinished. See [test results](windows/TEST_RESULTS.md).

## Optional Disposable Test Host

These helpers live in `windows/` and are explicit maintainer actions, never called
by the master installer:

- `enable-test-sandbox.ps1`: enable Windows Sandbox with administrator approval;
  report whether a restart is needed, never restart automatically.
- `prepare-sandbox-test.ps1`: create a sanitized Core package and read-only Sandbox
  mapping under `temp/installer-sandbox`. No live config, media or database is copied.
- `repair-test-host.ps1`: only with explicit permission, run Windows DISM RestoreHealth
  and then SFC. Stop if DISM fails or requests a restart. Review logs rather than
  assuming an exit code confirms all corruption is repaired.

Sandbox uses 8 GiB RAM and networking for downloads, with clipboard, camera,
microphone and printer redirection disabled. It does not persist the installed
application after closing. Copy the package into guest C:\FamilyFoto before running
the installer. This is a Core smoke-test environment,
not native People qualification or a replacement for persistent VM upgrade/reboot tests.

`test-sandbox-core.ps1` is an explicit disposable-guest harness. It checks the
Sandbox account and mapped input/output paths, generates a guest-only test password,
and runs Core setup followed by a first-login-page check. Only a dedicated empty
test-results folder should be mapped writable; never map the live repository writable.
The master accepts `-PasswordFromStdin` for unattended setup: supply one password
line through a protected input stream, never a command-line password or a log.
Interactive installation still prompts twice. Passwords are stored only as hashes
in application configuration.

For a reproducible Core test, run `powershell.exe -NoProfile -ExecutionPolicy Bypass
-File installer/windows/prepare-sandbox-test.ps1 -RunCoreTest` and open the reported `.wsb`.
The generated configuration runs the guest harness automatically. Inspect
`results/status.json`, `install.log`, `error.log`, and `smoke.json`. Never treat
merely opening Sandbox or an installer process exit as end-to-end qualification.

Python 3.12.10 is pinned for Windows binary compatibility; newer 3.12 security
releases are source-only. Selecting a maintained runtime/build and qualifying its
People wheels remains a release security task; this test pin is not a claim that
3.12.10 contains every subsequent security fix.
