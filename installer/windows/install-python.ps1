# Application-local CPython: no MSI/Burn registration, PATH or launcher changes.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'setup-common.ps1')

function Assert-PrivatePythonRuntime([string]$Directory) {
    foreach ($name in @('python.exe', 'python3.dll', 'python312.dll')) {
        $file = Join-Path $Directory $name
        Assert-SetupPath $file
        $signature = Get-AuthenticodeSignature -LiteralPath $file
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Python Software Foundation,') {
            throw "Python publisher verification failed: $name"
        }
    }
    # Ignore ambient PYTHONHOME/PYTHONPATH and user packages. Verify the full
    # runtime, not the embeddable distribution (which cannot supply our venvs).
    $probe = @'
import ctypes, ensurepip, pathlib, sqlite3, ssl, struct, sys, venv
root = pathlib.Path(sys.argv[1]).resolve()
assert sys.version_info[:3] == (3, 12, 10) and struct.calcsize('P') == 8
assert pathlib.Path(sys.base_prefix).resolve() == root
assert pathlib.Path(sys.executable).resolve() == root / 'python.exe'
assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) for m in (ctypes, ensurepip, sqlite3, ssl, venv))
'@
    Invoke-SetupCommand (Join-Path $Directory 'python.exe') @('-I', '-B', '-c', $probe, $Directory)
}

# The official PSF NuGet package, as Install-PrivatePythonRuntime downloads it. Shared with the
# setup window's temporary password-check runtime (setup-password.ps1).
$PythonNuGetUrl = 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg'
$PythonNuGetSha256 = '0EB85C2DFCCCCF1B17352DE4C397F69194035B7D37149EACC16F1147D93DE3B8'

# Extract a downloaded package's tools/ runtime into $Destination, a new folder, and verify it
# there, as Install-PrivatePythonRuntime does for runtime\python. Registers nothing: no
# MSI/Burn installer, PATH, launcher or file associations.
function Expand-PrivatePythonRuntime([string]$Archive, [string]$Destination) {
    Assert-SetupPath $Destination
    if (Test-Path -LiteralPath $Destination) { throw 'Python runtime already exists; inspect interrupted setup before retrying.' }
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($Destination))
    $stage = Join-Path $parent ('.python-stage-' + [guid]::NewGuid().ToString('N'))
    Set-SetupPrivateDirectory $stage
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $stage)
        $tools = Join-Path $stage 'tools'
        Assert-PrivatePythonRuntime $tools
        Assert-SetupPath $Destination
        [IO.Directory]::Move($tools, $Destination)
    } finally {
        $fullStage = [IO.Path]::GetFullPath($stage)
        if ((Split-Path -Parent $fullStage) -ne $parent -or (Split-Path -Leaf $fullStage) -notmatch '^\.python-stage-[0-9a-f]{32}$') {
            throw 'Unexpected Python staging path; cleanup refused.'
        }
        Assert-SetupPath $fullStage
        Remove-Item -LiteralPath $fullStage -Recurse -Force
    }
}

function Install-PrivatePythonRuntime([string]$Root) {
    $runtime = Join-Path $Root 'runtime'
    $target = Join-Path $runtime 'python'
    $cache = Join-Path $Root 'temp'
    $archive = Join-Path $cache 'python.3.12.10.nupkg'
    Assert-SetupPath $target
    Assert-SetupPath $cache
    if (Test-Path -LiteralPath $target) { throw 'Python runtime already exists; inspect interrupted setup before retrying.' }
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    # Official PSF NuGet package; a plain ZIP with a complete tools/ runtime.
    # No nuget client, shared Python registration or existing runtime is used.
    Get-SetupDownload 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg' $archive '0EB85C2DFCCCCF1B17352DE4C397F69194035B7D37149EACC16F1147D93DE3B8'
    $stage = Join-Path $runtime ('.python-stage-' + [guid]::NewGuid().ToString('N'))
    Set-SetupPrivateDirectory $stage
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stage)
        $tools = Join-Path $stage 'tools'
        Assert-PrivatePythonRuntime $tools
        Assert-SetupPath $target
        # Same-volume rename publishes only a verified runtime and refuses any
        # existing target, including a concurrent installer; never overwrite it.
        [IO.Directory]::Move($tools, $target)
    } finally {
        $fullStage = [IO.Path]::GetFullPath($stage)
        if ((Split-Path -Parent $fullStage) -ne [IO.Path]::GetFullPath($runtime) -or
            (Split-Path -Leaf $fullStage) -notmatch '^\.python-stage-[0-9a-f]{32}$') {
            throw 'Unexpected Python staging path; cleanup refused.'
        }
        Assert-SetupPath $fullStage
        Remove-Item -LiteralPath $fullStage -Recurse -Force
    }
    Write-Output "Private Python runtime ready: $target"
}

# Tests can dot-source the functions without provisioning anything.
if ($MyInvocation.InvocationName -ne '.') {
    Install-PrivatePythonRuntime (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)
}
