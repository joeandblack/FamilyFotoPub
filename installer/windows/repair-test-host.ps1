# Explicitly approved Windows repair prerequisite. Never restarts the host.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$output = Join-Path $root 'temp\installer-sandbox'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$status = Join-Path $output 'repair-status.json'
function Save-RepairStatus($value) {
    $value | ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
}
try {
    Save-RepairStatus @{stage='DISM';state='running';started=(Get-Date).ToString('o')}
    $dism = Start-Process -FilePath "$env:SystemRoot\System32\Dism.exe" -ArgumentList '/Online','/Cleanup-Image','/RestoreHealth','/NoRestart' -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $output 'repair-dism.log') -RedirectStandardError (Join-Path $output 'repair-dism-error.log')
    if ($dism.ExitCode -notin @(0,3010)) {
        Save-RepairStatus @{stage='DISM';state='failed';exitCode=$dism.ExitCode}
        exit 1
    }
    if ($dism.ExitCode -eq 3010) {
        Save-RepairStatus @{stage='DISM';state='restart-required';exitCode=$dism.ExitCode}
        exit 0
    }
    Save-RepairStatus @{stage='SFC';state='running';dismExitCode=$dism.ExitCode}
    $sfc = Start-Process -FilePath "$env:SystemRoot\System32\sfc.exe" -ArgumentList '/scannow' -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $output 'repair-sfc.log') -RedirectStandardError (Join-Path $output 'repair-sfc-error.log')
    Save-RepairStatus @{stage='SFC';state='finished-review-log';dismExitCode=$dism.ExitCode;sfcExitCode=$sfc.ExitCode;finished=(Get-Date).ToString('o')}
} catch {
    Save-RepairStatus @{state='failed';message=$_.Exception.Message}
    exit 1
}
