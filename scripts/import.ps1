$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
Push-Location $root
try {
  & $python -m app.imports.importer
  if ($LASTEXITCODE -ne 0) { throw 'Import paused or failed. Review logs and job status before retrying.' }
} finally { Pop-Location }
