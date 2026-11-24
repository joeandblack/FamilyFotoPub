<#
Run in an administrator PowerShell window. Downloads the backup's exact commit into a fresh
installation folder, then opens Windows Restore setup. No restore runs until
the user submits the setup window. Git is required. The public repository
downloads over anonymous HTTPS; no GitHub account is needed.
#>
[CmdletBinding()]
param([string]$InstallDirectory)
$ErrorActionPreference = 'Stop'
$commit = ''

function Assert-RestoreCommit([string]$Commit) {
    if ($Commit -cnotmatch '\A[0-9a-f]{40}\z') {
        throw 'This launcher has no verified code version. Obtain the matching FamilyFoto release; no files were downloaded.'
    }
}

function Get-RestoreSource([string]$Git, [string[]]$Options, [string]$Source, [string]$Commit) {
    Assert-RestoreCommit $Commit
    & $Git @Options init -- $Source
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the download folder.' }
    & $Git @Options -C $Source fetch --depth 1 https://github.com/joeandblack/FamilyFotoPub.git $Commit
    if ($LASTEXITCODE -ne 0) { throw 'Exact commit download failed. Check the network; no branch fallback is allowed.' }
    & $Git @Options -C $Source checkout --detach $Commit
    if ($LASTEXITCODE -ne 0) { throw 'Exact commit checkout failed.' }
    $actual = & $Git @Options -C $Source rev-parse --verify 'HEAD^{commit}'
    if ($LASTEXITCODE -ne 0 -or $actual -cne $Commit) { throw 'Downloaded commit differs from the backup.' }
}

function Get-RestoreGitArguments([string]$GhPath) {
    # Command-local helpers only: never store a token or change global Git settings.
    @('-c', 'credential.helper=', '-c', 'core.askPass=')
    if ($GhPath) {
        $helper = '!"' + $GhPath.Replace('\', '/') + '" auth git-credential'
        @('-c', "credential.helper=$helper")
    }
}

function Copy-RestoreDownload([string]$Source, [string]$Target) {
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        $destination = Join-Path $Target $item.Name
        if (Test-Path -LiteralPath $destination) { throw "Destination changed during download: $destination" }
        # Git pack files can be read-only. Cross-volume Move-Item copies them
        # then fails deleting the source; copy first and clean staging separately.
        Copy-Item -LiteralPath $item.FullName -Destination $destination -Recurse -Force
    }
}

function Assert-FreshRestoreFolder([string]$Path, [string]$Launcher) {
    if ($Path -notmatch '^[A-Za-z]:\\' -or $Path -match '[\s"]') {
        throw 'Choose a local drive path without spaces or quotes, for example D:\FamilyFotoRestore.'
    }
    $itemPath = $Path
    while ($itemPath) {
        if (Test-Path -LiteralPath $itemPath) {
            $item = Get-Item -LiteralPath $itemPath -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'The installation path must contain only real directories, not links or junctions.'
            }
        }
        $itemPath = Split-Path -Parent $itemPath
    }
    if (Test-Path -LiteralPath $Path) {
        $other = @(Get-ChildItem -LiteralPath $Path -Force | Where-Object { $_.FullName -ne $Launcher })
        if ($other.Count) {
            throw 'This folder is not empty. Choose a fresh folder. For an interrupted restore, use its existing installer/windows/install.ps1 -Mode Restore.'
        }
    }
}

# Dot-sourcing exposes only the read-only validation helper for fixture tests.
if ($MyInvocation.InvocationName -eq '.') { return }
Assert-RestoreCommit $commit
if ($env:OS -ne 'Windows_NT') { throw 'Run this launcher on Windows.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as administrator using the Windows account that will operate FamilyFoto, then run this script again.'
}
$git = (Get-Command git.exe -ErrorAction Stop).Source
$ghCommand = Get-Command gh.exe -ErrorAction SilentlyContinue
$gitOptions = @(Get-RestoreGitArguments $(if ($ghCommand) { $ghCommand.Source } else { '' }))
$launcher = [IO.Path]::GetFullPath($PSCommandPath)
if (-not $InstallDirectory) {
    $default = (Get-Location).ProviderPath
    $answer = Read-Host "Installation folder [$default]"
    $InstallDirectory = if ([string]::IsNullOrWhiteSpace($answer)) { $default } else { $answer.Trim().Trim('"') }
}
$target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InstallDirectory)
Assert-FreshRestoreFolder $target $launcher
$drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($target))
if ($drive.DriveFormat -ne 'NTFS' -or $drive.DriveType -ne [IO.DriveType]::Fixed) {
    throw 'Install the application on a local fixed NTFS disk. Media can be on a separate disk.'
}
if ($drive.AvailableFreeSpace -lt 10GB) { throw 'At least 10 GB free space is required on the installation disk.' }

$stage = Join-Path ([IO.Path]::GetTempPath()) ('familyfoto-download-' + [guid]::NewGuid().ToString('N'))
$source = Join-Path $stage 'source'
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    Write-Host "Downloading FamilyFoto commit $commit from GitHub..."
    $oldPrompt = $env:GIT_TERMINAL_PROMPT
    try {
        $env:GIT_TERMINAL_PROMPT = '0'
        Get-RestoreSource $git $gitOptions $source $commit
    } finally {
        $env:GIT_TERMINAL_PROMPT = $oldPrompt
    }
    # Refuse changes to the destination that happened during the download.
    Assert-FreshRestoreFolder $target $launcher
    if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
    Copy-RestoreDownload $source $target
} finally {
    $resolved = [IO.Path]::GetFullPath($stage)
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolved.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolved) -match '^familyfoto-download-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Write-Host "Installation folder: $target"
Write-Host 'Opening Restore setup. Select your backup, backup password and media destination there.'
$shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
& $shell -NoProfile -File (Join-Path $target 'installer\windows\install.ps1') -Mode Restore
if ($LASTEXITCODE -ne 0) {
    throw 'Setup stopped. Keep this folder and resume using installer/windows/install.ps1 -Mode Restore; do not download over it.'
}
