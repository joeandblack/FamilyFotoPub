# Verify graceful stop/start of the disposable installation, not the live server.
$ErrorActionPreference = 'Stop'
if ($env:USERNAME -ne 'WDAGUtilityAccount' -or $PSScriptRoot -ne 'C:\FamilyFoto\installer\windows') {
    throw 'Disposable Sandbox installation only.'
}
$root = 'C:\FamilyFoto'
try {
    $shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'FamilyFoto Server.lnk'
    if (-not (Test-Path $shortcut)) { throw 'Desktop launcher missing.' }
    [IO.File]::WriteAllText("$root\data\DESKTOP-SERVER-STOP", '')
    $stopped = $false
    for ($i=0; $i -lt 60; $i++) {
        if ((Get-ScheduledTask -TaskName 'FamilyFoto Desktop Server').State -eq 'Ready') {
            $stopped=$true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $stopped) { throw 'Graceful stop timed out; no process was force-stopped.' }
    # The controller normally clears this cooperative marker before starting.
    if (Test-Path "$root\data\DESKTOP-SERVER-STOP") { Remove-Item -LiteralPath "$root\data\DESKTOP-SERVER-STOP" }
    Start-ScheduledTask -TaskName 'FamilyFoto Desktop Server'
    # Probe our own loopback HTTPS server inside the disposable sandbox; the private CA is not trusted here.
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
    $ready = $false
    for ($i=0; $i -lt 60; $i++) {
        try {
            $r=Invoke-WebRequest https://127.0.0.1:4340/login -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ready=$true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $ready) { throw 'Server failed to return after restart.' }
    @{passed=$true;check='Desktop shortcut exists; graceful stop and start; login page returns'} |
        ConvertTo-Json | Set-Content C:\TestResults\restart.json
} catch {
    @{passed=$false;detail=$_.Exception.Message} | ConvertTo-Json | Set-Content C:\TestResults\restart.json
    exit 1
}
