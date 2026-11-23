# Build a disposable Core test input. Never map the live repository into the guest.
param([switch]$RunCoreTest, [string]$Python, [switch]$AllowDirty)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$stage = Join-Path $root ('temp\installer-sandbox\test-' + [Guid]::NewGuid().ToString('N'))
$package = Join-Path $stage 'input\FamilyFoto'
# Use the same audited payload as release builds, not a separate copy list.
if (-not $Python) {
    $Python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { $Python = 'python' }
}
$buildArgs = @((Join-Path $root 'installer\release.py'), 'build', '--output', (Join-Path $stage 'artifacts'))
if ($AllowDirty) { $buildArgs += '--allow-dirty' }
$archive = & $Python @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed; Sandbox was not prepared' }
Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $stage 'input')
# Only this sanitized input directory is mapped, read-only. Install into guest C:.
$escaped = [Security.SecurityElement]::Escape((Join-Path $stage 'input'))
$outputMapping = ''
$logon = ''
if ($RunCoreTest) {
    $results = Join-Path $stage 'results'
    New-Item -ItemType Directory -Path $results | Out-Null
    $escapedResults = [Security.SecurityElement]::Escape($results)
    $outputMapping = "<MappedFolder><HostFolder>$escapedResults</HostFolder><SandboxFolder>C:\TestResults</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>"
    $logon = '<LogonCommand><Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\TestInput\FamilyFoto\installer\windows\test-sandbox-core.ps1</Command></LogonCommand>'
}
@"
<Configuration>
  <VGpu>Disable</VGpu>
  <MemoryInMB>8192</MemoryInMB>
  <Networking>Enable</Networking>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <PrinterRedirection>Disable</PrinterRedirection>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <MappedFolders><MappedFolder>
    <HostFolder>$escaped</HostFolder>
    <SandboxFolder>C:\TestInput</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>$outputMapping</MappedFolders>
  $logon
</Configuration>
"@ | Set-Content -LiteralPath (Join-Path $stage 'FamilyFoto-Core.wsb') -Encoding UTF8
Write-Output "Prepared: $stage"
Write-Output 'Copy C:\TestInput\FamilyFoto to C:\FamilyFoto inside the guest before installation.'
Write-Output 'Networking is enabled for dependency downloads; do not access the live server from the guest.'
if ($RunCoreTest) { Write-Output 'Opening this .wsb starts the disposable Core test. Only the dedicated results folder is writable from the guest.' }
