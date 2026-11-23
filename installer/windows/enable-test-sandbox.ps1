# Explicit host prerequisite for disposable installer testing. Never restarts Windows.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$output = Join-Path $root 'temp\installer-sandbox'
New-Item -ItemType Directory -Path $output -Force | Out-Null
try {
    $feature = Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM
    if ($feature.State -eq 'Enabled') {
        $result = @{state='enabled'; restartNeeded=$false}
    } else {
        $enabled = Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All -NoRestart
        $result = @{state='enabled'; restartNeeded=[bool]$enabled.RestartNeeded}
    }
    $result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'feature-result.json') -Encoding UTF8
} catch {
    @{state='failed'; message=$_.Exception.Message} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $output 'feature-result.json') -Encoding UTF8
    exit 1
}
