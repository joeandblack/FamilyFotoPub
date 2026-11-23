# Optional fresh native People setup. Plan is read-only; Core must already exist.
[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Install', 'Verify')][string]$Mode = 'Plan',
    [switch]$ResearchOnly,
    [ValidateRange(49152, 65535)][int]$Port = 54341
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ($Mode -eq 'Plan') {
    Write-Output "Native People setup plan: $root"
    Write-Output 'Requires completed Core setup, its own Python 3.12 venv and empty local database.'
    Write-Output 'Installs hash-pinned InsightFace CPU dependencies and approved research model assets.'
    Write-Output "Creates an empty model-scoped catalog on People port $Port. No media paths required."
    Write-Output 'No Docker, service startup, scans, automatic matching, media or existing People migration.'
    Write-Output 'Install requires -ResearchOnly after reviewing the model terms. Existing catalogs are refused.'
    return
}
. (Join-Path $PSScriptRoot 'setup-common.ps1')
Assert-SetupPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Complete Core installation first.' }
$arguments = @((Join-Path $PSScriptRoot 'people.py'), '--mode', $Mode.ToLower(), '--port', "$Port")
if ($ResearchOnly) { $arguments += '--research-only' }
Invoke-SetupCommand $python $arguments
