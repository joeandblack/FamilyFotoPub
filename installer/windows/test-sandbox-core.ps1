# Explicit disposable test harness. Never execute this on a real installation host.
$ErrorActionPreference = 'Stop'
if ($env:USERNAME -ne 'WDAGUtilityAccount' -or -not (Test-Path 'C:\TestInput\FamilyFoto') -or
    -not (Test-Path 'C:\TestResults')) { throw 'This harness requires the prepared Windows Sandbox.' }
$root = 'C:\FamilyFoto'
if (Test-Path $root) { throw 'Fresh test only: C:\FamilyFoto already exists.' }
function Status([string]$state, [string]$message) {
    @{state=$state;message=$message;time=(Get-Date).ToString('o')} | ConvertTo-Json |
        Set-Content C:\TestResults\status.json
}
try {
    Status 'running' 'Copying sanitized application'
    Copy-Item -LiteralPath C:\TestInput\FamilyFoto -Destination $root -Recurse
    . "$root\installer\windows\setup-common.ps1"
    Set-SetupPrivateDirectory 'C:\TestCredentials'
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    [Convert]::ToBase64String($bytes) | Set-Content C:\TestCredentials\password.txt -Encoding ASCII
    Status 'running' 'Installing Core in guest only'
    $process = Start-Process powershell.exe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "$root\installer\windows\install.ps1", '-Mode', 'Install', '-PasswordFromStdin', '-Start') -PassThru -Wait -WindowStyle Hidden `
        -RedirectStandardInput C:\TestCredentials\password.txt `
        -RedirectStandardOutput C:\TestResults\install.log -RedirectStandardError C:\TestResults\error.log
    if ($process.ExitCode -ne 0) { throw "Installer exited $($process.ExitCode); see logs." }
    Status 'installed' 'Core installation succeeded; checking first launch'
    # Probe our own loopback HTTPS server inside the disposable sandbox; the private CA is not trusted here.
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
    $ready = $false
    for ($i=0; $i -lt 60; $i++) {
        try {
            $response = Invoke-WebRequest https://127.0.0.1:4340/login -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { $ready=$true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $ready) { throw 'Installed app did not serve the login page.' }
    & "$root\.venv\Scripts\python.exe" "$root\installer\windows\smoke-sandbox.py"
    if ($LASTEXITCODE -ne 0) { throw 'HTTP smoke checks failed; see smoke.json.' }
    & "$root\installer\windows\restart-sandbox.ps1"
    if ($LASTEXITCODE -ne 0) { throw 'Server restart check failed; see restart.json.' }
    Status 'passed-core' 'Installation, HTTP and server restart checks passed. Check smoke.json for skipped storage/import checks; full VM qualification still required.'
} catch {
    Status 'failed' $_.Exception.Message
    exit 1
}
