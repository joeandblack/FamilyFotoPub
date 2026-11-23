[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Root, [Parameter(Mandatory=$true)][string]$Receipt, [switch]$Elevate, [string]$BackupDirectory)
$ErrorActionPreference = 'Stop'
if ($Root -match '["\r\n]' -or $Receipt -match '["\r\n]' -or $BackupDirectory -match '["\r\n]') { throw 'Invalid setup argument.' }
if ($Elevate) {
    $shell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = "-NoProfile -File `"$PSCommandPath`" -Root `"$Root`" -Receipt `"$Receipt`""
    if ($BackupDirectory) { $arguments += " -BackupDirectory `"$BackupDirectory`"" }
    $child = Start-Process -FilePath $shell -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    exit $child.ExitCode
}
$env:FAMILYFOTO_PAYLOAD_RECEIPT = $Receipt
$mode = 'Install'
if (Test-Path -LiteralPath (Join-Path $Root 'data\setup\install.json')) { $mode = 'Resume' }
if ($BackupDirectory) {
    $env:FAMILYFOTO_SETUP_BACKUP = $BackupDirectory
    $mode = 'Restore'
}
& (Join-Path $Root 'installer\windows\install.ps1') -Mode $mode
