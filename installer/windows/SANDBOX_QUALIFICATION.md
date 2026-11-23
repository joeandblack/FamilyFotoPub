# Windows Sandbox qualification

Date: 2026-11-22. Baseline: develop at
`8521eecf7f226eeb2e2dad1c211b5d4f1d8dd817`.

## Scope

Windows 11 Enterprise build 26100, AMD64, PowerShell 5.1, with no Python or
Git initially available. Only dedicated input (read-only) and results folders
were shared. Guest networking and clipboard were disabled. No live or test
server directories, databases, media or backups were exposed or changed.

This is internal qualification, not approval to redistribute the artifacts.
The manifest used pinned real payloads and internal test terms, not a finished
release agreement. Hosted online acquisition, the packaged EXE and its interactive
consent/progress UI were not exercised.

## Completed checks

- Private Python 3.12.10: pinned archive digest and valid Python Software
  Foundation signatures on the executable and Python DLLs.
- Standard-library-only bootstrap imports, consent refusal before preparation,
  offline acquisition, materialization, staging and repeated preparation.
- Real Core installation with PostgreSQL 17.11, private Python/venv, hash-locked
  Python dependencies, FFmpeg/ffprobe, configuration/TLS, schema migrations,
  restricted database role and launcher. Core verification passed.
- A second named Core installation from the corrected application archive passed
  using its own database cluster, ports and launcher.
- With explicit owner approval of research-only model terms, InsightFace and the
  pinned buffalo_l model installed offline. Both dependency checks passed; model
  digests, provider environment and fresh People catalog verification passed.
  Matching remained review-only and automatic startup disabled. This does not
  qualify real-photo detection accuracy or a restored People catalog.
- Release-packaging regression tests: 11 passed, 1 deselected. The pre-existing
  extracted-release smoke test was excluded because its audit hook rejects the
  Windows `whoami` subprocess used by private ACL checks; this is not a full-suite
  pass.

## Packaging defect found

The release allowlist omitted the entire `fastbackup` runtime. Core installation
passed, but the synthetic backup harness failed to import `fastbackup` from the
installed release. Include its Python runtime files and validate its CLI entry
point. The regression checks all tracked runtime modules are present while tests
and non-runtime documents remain excluded.

## Restore qualification blocked

After correcting the archive, storage enrollment failed before synthetic media
or catalog records were created. Sandbox exposes C: as a logical NTFS disk, but
`Get-Partition -DriveLetter C` returns no `MSFT_Partition` objects.

A disposable guest-only VHDX was created, attached, formatted and assigned T:.
It too appeared in `Win32_LogicalDisk`, but `Get-Partition -DriveLetter T` returned
the same error. FamilyFoto's real disk-identity checks therefore cannot enroll
these volumes. No application check was bypassed or weakened.

Full media backup/restore, authenticated restored-media reads and storage-boundary
recovery remain unqualified. Continue those tests in a disposable full Windows VM
with normal partition APIs and only synthetic media; do not interpret successful
Core provisioning as successful end-to-end restore.
