$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$config = Get-Content "$root\config\local.json" -Raw | ConvertFrom-Json
if (Get-NetTCPConnection -LocalPort $config.port -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port already in use. No second server was started.'
}
$module = if ($config.live_existing_catalog) { 'app.service.live' } else { 'app.service.desktop' }
$process = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList '-m',$module -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\logs\desktop-server.stdout.log" -RedirectStandardError "$root\logs\desktop-server.log" -PassThru
# Wait only for the web process, not independent workers it launched.
$process.WaitForExit()
exit $process.ExitCode
