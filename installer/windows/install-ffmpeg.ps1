# Fresh-install local video tools.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'setup-common.ps1')
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$version='9.0.2'
$expected='60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba'
$archive=Join-Path $root "temp\ffmpeg-$version-essentials_build.zip"
$unpack=Join-Path $root "temp\ffmpeg-$version-extracted"
$bin=Join-Path $root 'runtime\ffmpeg\bin'
if (Test-Path -LiteralPath $bin) {throw 'FFmpeg already provisioned; review an upgrade separately.'}
New-Item -ItemType Directory -Path (Split-Path $archive) -Force | Out-Null
# The release does not include FFmpeg: setup downloads gyan.dev's build here, even when installing
# from prepared or offline payloads, and checks it against the pinned SHA-256.
try {
    Get-SetupDownload "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-$version-essentials_build.zip" $archive $expected -Network
} catch {
    throw "FFmpeg is downloaded from www.gyan.dev during setup and could not be: $($_.Exception.Message) Connect to the internet and choose Try again."
}
if (Test-Path -LiteralPath $unpack) {throw 'Extraction directory exists; inspect it before retrying.'}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip=[IO.Compression.ZipFile]::OpenRead($archive)
try {
    foreach ($entry in $zip.Entries) {
        $resolved=[IO.Path]::GetFullPath((Join-Path $unpack $entry.FullName))
        if (-not $resolved.StartsWith($unpack+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Archive path outside extraction directory.'}
    }
} finally {$zip.Dispose()}
Expand-Archive -LiteralPath $archive -DestinationPath $unpack
New-Item -ItemType Directory -Path $bin -Force | Out-Null
foreach ($name in @('ffmpeg.exe','ffprobe.exe')) {
    $source=Get-ChildItem -LiteralPath $unpack -Recurse -File -Filter $name
    if (@($source).Count -ne 1) {throw "Expected exactly one $name"}
    Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $bin $name)
}
& (Join-Path $bin 'ffmpeg.exe') -version | Select-Object -First 1
Write-Output 'FFmpeg provisioned locally. No PATH, startup, database or media changes.'
