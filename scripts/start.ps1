$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Run the installation instructions first.' }
. (Join-Path $PSScriptRoot 'instance-names.ps1')
$config=Get-Content "$root\config\local.json" -Raw | ConvertFrom-Json
Start-Service (Get-FamilyFotoConfigNames $config).DatabaseService
if (Get-NetTCPConnection -LocalPort $config.port -State Listen -ErrorAction SilentlyContinue) { throw 'Configured web port already in use; no duplicate started.' }
Start-Process -FilePath $python -ArgumentList '-m','app.service.server' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\logs\web-console.log" -RedirectStandardError "$root\logs\web-error.log"
$scheme = if ($null -ne $config.tls) { 'https' } else { 'http' }
Write-Output "FamilyFoto launch requested at ${scheme}://$($config.host):$($config.port)/. Check logs for startup success. Queued uploads are dispatched when idle; server-folder imports require Start."
