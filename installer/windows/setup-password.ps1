<#
Check a backup's administrator password before anything is installed (docs/BACKUP.md, "Backup password",
setup windows). A fresh Windows PC has no Python, and Windows PowerShell's .NET has
no scrypt, so the setup window prepares a private, temporary Python first:

- the official PSF NuGet package, as Core's own runtime (install-python.ps1): checked against its
  pinned hash, its signatures verified, extracted into an installer-owned folder under temp, and
  registered nowhere (no MSI/Burn installer, PATH, launcher or file associations);
- the pinned cryptography and cffi wheels, each checked against its pinned hash, installed by that
  runtime's own pip into the same folder with --require-hashes, --no-index and no caches;
- installer/password_check.py, which runs the restore's own password code (app.backup.passwords).

Nothing else changes before Start: no Core files, settings, journal, database or service. The folder
is removed when the window closes; a folder left by a crash is removed by the next setup once its
owner process is gone. The password goes to the check on its stdin, as one frame, only.

Run with -PrepareFor ROOT to prepare one and print its folder as one "@@ {json}" line.
#>
[CmdletBinding()]
param([string]$PrepareFor)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'install-python.ps1')

$PasswordCheckPrefix = 'password-check-'
$PasswordCheckWheels = @(
    @{ File = 'cryptography-44.0.3-cp39-abi3-win_amd64.whl'; Requirement = 'cryptography==44.0.3'
       Url = 'https://files.pythonhosted.org/packages/c9/ad/51f212198681ea7b0deaaf8846ee10af99fba4e894f67b353524eab2bbe5/cryptography-44.0.3-cp39-abi3-win_amd64.whl'
       Sha256 = '5D186F32E52E66994DCE4F766884BCB9C68B8DA62D61D9D215BFE5FB56D21334' },
    @{ File = 'cffi-2.1.1-cp312-cp312-win_amd64.whl'; Requirement = 'cffi==2.1.1'
       Url = 'https://files.pythonhosted.org/packages/d9/79/615cc094e2fb508cade7de88d3b4f6c4ec2bab695c97bce9153dc65aadf5/cffi-2.1.1-cp312-cp312-win_amd64.whl'
       Sha256 = 'F53E442B08449D42821FA4A4FBA000095AF9F62742A500F978A9F557EC44339A' }
)
$PasswordFrameLimit = 8192

function Get-SetupPasswordTemp([string]$Root) { Join-Path ([IO.Path]::GetFullPath($Root)) 'temp' }

function Test-SetupPasswordRuntimePath([string]$Root, [string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    (Split-Path -Parent $full) -eq (Get-SetupPasswordTemp $Root) -and (Split-Path -Leaf $full) -match "^$PasswordCheckPrefix[0-9a-f]{32}$"
}

function Remove-SetupPasswordRuntime([string]$Root, [string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    if (-not (Test-SetupPasswordRuntimePath $Root $Path)) { throw 'Unexpected password-check folder; cleanup refused.' }
    Assert-SetupPath $Path
    Remove-Item -LiteralPath $Path -Recurse -Force
}

# Folders left by a setup that crashed: only ours, and only once their owner process is gone.
function Clear-SetupPasswordRuntimes([string]$Root) {
    $temp = Get-SetupPasswordTemp $Root
    if (-not (Test-Path -LiteralPath $temp)) { return }
    foreach ($folder in @(Get-ChildItem -LiteralPath $temp -Directory -Filter "$PasswordCheckPrefix*" -ErrorAction SilentlyContinue)) {
        if (-not (Test-SetupPasswordRuntimePath $Root $folder.FullName)) { continue }
        $owner = 0
        try { $owner = [int](Get-Content -LiteralPath (Join-Path $folder.FullName 'owner.txt') -Raw).Trim() } catch { }
        if ($owner -and (Get-Process -Id $owner -ErrorAction SilentlyContinue)) { continue }
        try { Remove-SetupPasswordRuntime $Root $folder.FullName } catch { }
    }
}

# A new runtime, owned by process $Owner (the setup window). Returns its folder.
function New-SetupPasswordRuntime([string]$Root, [int]$Owner = $PID) {
    $temp = Get-SetupPasswordTemp $Root
    Assert-SetupPath $temp
    New-Item -ItemType Directory -Path $temp -Force | Out-Null
    Clear-SetupPasswordRuntimes $Root
    # Downloads stay in installer-owned storage. Core's own Python step reuses this verified package.
    $archive = Join-Path $temp 'python.3.12.10.nupkg'
    Get-SetupDownload $PythonNuGetUrl $archive $PythonNuGetSha256
    $wheels = Join-Path $temp 'password-check-wheels'
    Assert-SetupPath $wheels
    New-Item -ItemType Directory -Path $wheels -Force | Out-Null
    foreach ($wheel in $PasswordCheckWheels) { Get-SetupDownload $wheel.Url (Join-Path $wheels $wheel.File) $wheel.Sha256 }
    $folder = Join-Path $temp ($PasswordCheckPrefix + [guid]::NewGuid().ToString('N'))
    Set-SetupPrivateDirectory $folder
    try {
        Set-Content -LiteralPath (Join-Path $folder 'owner.txt') -Value ([string]$Owner) -Encoding ASCII
        $python = Join-Path $folder 'python'
        Expand-PrivatePythonRuntime $archive $python
        $exe = Join-Path $python 'python.exe'
        $requirements = Join-Path $folder 'requirements.txt'
        Set-Content -LiteralPath $requirements -Encoding ASCII -Value @($PasswordCheckWheels | ForEach-Object { "$($_.Requirement) --hash=sha256:$($_.Sha256.ToLowerInvariant())" })
        # The runtime's own bundled pip, then only the verified wheels: no index, no cache, no user or environment configuration.
        Invoke-SetupCommand $exe @('-I', '-m', 'ensurepip', '--default-pip') | ForEach-Object { Write-Host $_ }
        Invoke-SetupCommand $exe @('-I', '-m', 'pip', 'install', '--isolated', '--disable-pip-version-check', '--no-cache-dir',
            '--no-index', '--find-links', $wheels, '--only-binary', ':all:', '--no-deps', '--require-hashes',
            '--target', (Join-Path $folder 'site'), '-r', $requirements) | ForEach-Object { Write-Host $_ }
    } catch {
        Remove-SetupPasswordRuntime $Root $folder
        throw
    }
    $folder
}

function Start-SetupPasswordProcess([Diagnostics.ProcessStartInfo]$Info) {
    # Windows PowerShell's .NET Framework uses this encoding to construct stdin
    # and can emit a BOM during Start(), before our frame is written.
    $previous = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
        [Diagnostics.Process]::Start($Info)
    } finally { [Console]::InputEncoding = $previous }
}

# Run installer/password_check.py with the frame on its stdin. Returns its answer.
function Invoke-SetupPasswordCheck([string]$Root, [string]$Runtime, [string]$Backup, [byte[]]$Frame, [int]$TimeoutMilliseconds = 120000, [string[]]$Test = @()) {
    if ($Test.Count -eq 0 -and -not (Test-SetupPasswordRuntimePath $Root $Runtime)) { throw 'Unexpected password-check folder.' }
    $info = New-Object Diagnostics.ProcessStartInfo (Join-Path $Runtime 'python\python.exe')
    $info.Arguments = ('-I -B "{0}" --site "{1}" --backup "{2}"' -f (Join-Path $Root 'installer\password_check.py'), (Join-Path $Runtime 'site'), $Backup.TrimEnd('\'))
    # Tests only: a stand-in helper (program, then its arguments), for one that never exits.
    if ($Test.Count -ge 1) { $info.FileName = $Test[0]; $info.Arguments = ($Test | Select-Object -Skip 1) -join ' ' }
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true; $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
    $process = Start-SetupPasswordProcess $info
    try {
        $process.StandardInput.BaseStream.Write($Frame, 0, $Frame.Length)
        # Close the raw pipe, not its unused text writer (which can append a BOM).
        $process.StandardInput.BaseStream.Close()
        # Both streams drain on their own while this waits with a deadline: a helper that hangs, or
        # fills stderr, can neither block the read nor deadlock the other stream.
        $outputTask = $process.StandardOutput.ReadToEndAsync(); $errorTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try { $process.Kill() } catch { }
            [void]$process.WaitForExit(10000)
            throw 'The password check did not finish.'
        }
        # The process has ended, so its pipes close; a grandchild holding them open gets a short wait.
        if (-not [Threading.Tasks.Task]::WaitAll(@($outputTask, $errorTask), 10000)) { throw 'The password check did not finish.' }
        $output = $outputTask.Result
    } finally { $process.Dispose() }
    $line = @($output -split "`r?`n" | Where-Object { $_.Trim() }) | Select-Object -Last 1
    try { $line | ConvertFrom-Json } catch { [pscustomobject]@{ ok = $false; reason = 'prerequisite'; message = 'The password check gave no answer.' } }
}

# Exactly one frame from this process's own stdin, byte by byte: a 4-byte big-endian length, then at
# most $PasswordFrameLimit bytes. Read before any native child starts, so none can inherit it unread.
function Read-SetupPasswordFrame {
    $stream = [Console]::OpenStandardInput()
    $read = {
        param([int]$Count)
        $buffer = New-Object byte[] $Count; $offset = 0
        while ($offset -lt $Count) {
            $got = $stream.Read($buffer, $offset, $Count - $offset)
            if ($got -le 0) { throw 'The backup password message was cut short. Nothing was changed.' }
            $offset += $got
        }
        , $buffer
    }
    $header = & $read 4
    $length = ([int]$header[0] -shl 24) -bor ([int]$header[1] -shl 16) -bor ([int]$header[2] -shl 8) -bor [int]$header[3]
    if ($length -lt 2 -or $length -gt $PasswordFrameLimit) { throw 'The backup password message is not valid. Nothing was changed.' }
    $payload = & $read $length
    , [byte[]]($header + $payload)
}

# Windows command-line quoting, as setup-run.cs does: backslashes are literal unless before a quote.
function ConvertTo-SetupArgument([string]$Value) {
    $quoted = New-Object Text.StringBuilder '"'; $slashes = 0
    foreach ($c in $Value.ToCharArray()) {
        if ($c -eq '\') { $slashes++; continue }
        if ($c -eq '"') { [void]$quoted.Append('\', $slashes * 2 + 1); [void]$quoted.Append('"') }
        else { [void]$quoted.Append('\', $slashes); [void]$quoted.Append($c) }
        $slashes = 0
    }
    [void]$quoted.Append('\', $slashes * 2); [void]$quoted.Append('"')
    $quoted.ToString()
}

# Run a native child with the frame on its own private stdin pipe; its output goes where ours goes.
# Returns its exit code. No other child gets this pipe.
function Invoke-SetupPasswordChild([string]$Program, [string[]]$Arguments, [byte[]]$Frame) {
    $info = New-Object Diagnostics.ProcessStartInfo $Program
    $info.Arguments = (@($Arguments | ForEach-Object { ConvertTo-SetupArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $true
    $info.WorkingDirectory = (Get-Location).Path
    $process = Start-SetupPasswordProcess $info
    try {
        $process.StandardInput.BaseStream.Write($Frame, 0, $Frame.Length)
        $process.StandardInput.BaseStream.Close()
        $process.WaitForExit()
        $process.ExitCode
    } finally { $process.Dispose() }
}

if ($PrepareFor -and $MyInvocation.InvocationName -ne '.') {
    $owner = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
    $folder = New-SetupPasswordRuntime $PrepareFor ([int]$owner)
    Write-Output ('@@ ' + (@{ password_runtime = $folder } | ConvertTo-Json -Compress))
}
