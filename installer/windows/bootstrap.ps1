<# Release builder embeds this script and a pinned bootstrap.json in Setup.exe.
   No provisioning/elevation occurs here. Downloads are bootstrap-only. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Descriptor, [string]$BundleRoot)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$config = Get-Content -LiteralPath $Descriptor -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.schema_version -ne 1 -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Unsupported bootstrap platform/version.' }
if ([int](Get-CimInstance Win32_OperatingSystem).BuildNumber -lt 22000) { throw 'Windows 11 x64 is required.' }
if ($config.mode -notin @('online', 'offline')) { throw 'Unknown delivery mode.' }
Write-Output $config.bootstrap_notices
$base = Join-Path $env:LOCALAPPDATA ('FamilyFoto\Setup\' + $config.manifest_sha256)
if ($config.manifest_sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid release pin.' }
function Assert-BootstrapPath([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Linked bootstrap paths are refused.'
        }
        $cursor = Split-Path -Parent $cursor
    }
}
Assert-BootstrapPath $base
New-Item -ItemType Directory -Path $base -Force | Out-Null
$bootstrapLock = [IO.File]::Open((Join-Path $base 'bootstrap.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
function Get-BootstrapPayload($Item, [string]$Name) {
    if ($Item.sha256 -notmatch '^[a-f0-9]{64}$' -or -not $Item.url.StartsWith('https://')) { throw 'Invalid bootstrap payload.' }
    $path = Join-Path $base $Name
    Assert-BootstrapPath $path
    if (-not (Test-Path -LiteralPath $path)) {
        $partial = "$path.$([guid]::NewGuid().ToString('N')).partial"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            if ($config.mode -eq 'offline') {
                if (-not $BundleRoot -or $Item.bundle_path -match '(^[/\\]|\.\.|:)') { throw 'Missing or invalid offline bootstrap input.' }
                Copy-Item -LiteralPath (Join-Path $BundleRoot $Item.bundle_path) -Destination $partial -ErrorAction Stop
            } else {
                Invoke-WebRequest -Uri $Item.url -OutFile $partial -UseBasicParsing
            }
            if ((Get-FileHash -LiteralPath $partial).Hash -ne $Item.sha256) { throw 'Bootstrap checksum mismatch.' }
            Move-Item -LiteralPath $partial -Destination $path
        } finally { if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial } }
    }
    if ((Get-FileHash -LiteralPath $path).Hash -ne $Item.sha256) { throw 'Cached bootstrap checksum mismatch.' }
    return $path
}
function Expand-Bootstrap([string]$Archive, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) { throw 'Unexpected bootstrap extraction directory.' }
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $prefix = [IO.Path]::GetFullPath($Destination) + '\'
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
                $entry.FullName.Contains(':') -or $entry.FullName.StartsWith('/') -or
                (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Unsafe bootstrap archive.' }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $Destination)
}
$pythonZip = Get-BootstrapPayload $config.python 'python.nupkg'
$engineZip = Get-BootstrapPayload $config.engine 'engine.zip'
# Fresh extraction avoids trusting an interrupted or modified executable tree.
$session = Join-Path $base ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $session | Out-Null
Expand-Bootstrap $pythonZip (Join-Path $session 'python')
Expand-Bootstrap $engineZip (Join-Path $session 'engine')
$python = Join-Path $session 'python\tools\python.exe'
foreach ($name in @('python.exe', 'python3.dll', 'python312.dll')) {
    $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $session "python\tools\$name")
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Python Software Foundation,') { throw 'Python publisher verification failed.' }
}
Push-Location (Join-Path $session 'engine')
try {
    $engineArgs = @('--descriptor', $Descriptor, '--attempt', (Join-Path $base ('attempt-' + $config.mode)))
    if ($config.mode -eq 'offline') { $engineArgs += @('--bundle', $BundleRoot) }
    & $python -E -s -B -m installer.windows.online_setup @engineArgs
    if ($LASTEXITCODE -ne 0) { throw "Setup stopped (exit $LASTEXITCODE). Verified downloads were retained." }
} finally {
    Pop-Location
    $bootstrapLock.Dispose()
    $fullSession = [IO.Path]::GetFullPath($session)
    if ((Split-Path -Parent $fullSession) -ne [IO.Path]::GetFullPath($base) -or
        (Split-Path -Leaf $fullSession) -notmatch '^[a-f0-9]{32}$') { throw 'Unexpected bootstrap cleanup path.' }
    Assert-BootstrapPath $fullSession
    Remove-Item -LiteralPath $fullSession -Recurse -Force
}
