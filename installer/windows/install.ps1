<#
Windows Core setup coordinator in installer/windows/. Default mode is read-only Plan.
Run Install/Resume/Restore in elevated PowerShell as the intended Windows operator.
This is not an updater. Installation asks whether to install People; LAN TLS/firewall setup is separate.

Install asks whether this is a new library or a restore from a backup. Restore installs Core (if it
isn't yet) and then restores the backup into it in one go (installer/windows/restore.py); running
-Mode Restore again carries on from where it stopped.

-Instance NAME makes a second installation on a computer that already has one: its own database
service, server task, desktop shortcut and sign-in cookie, on the ports given with -DatabasePort,
-WebPort and -PeoplePort. Without -Instance, the names and ports are the original ones.

-BackupPasswordFromStdin reads one framed backup password (app.backup.passwords.frame: a 4-byte
big-endian length, then UTF-8 JSON) from stdin before anything else runs. It is the FamilyFoto
administrator password the backup was made with. A fresh restore checks it before Core is
installed (setup-password.ps1), and restore.py gets it on its own private pipe. It is a
different message from -PasswordFromStdin, the new account password of a new library.
-PasswordCheckRuntime reuses the setup window's prepared password-check folder.
#>
[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Install', 'Resume', 'Restore', 'Verify', 'Upgrade')][string]$Mode = 'Plan',
    [string]$Python,
    [switch]$PasswordFromStdin,
    [switch]$WithPeople,
    [switch]$WithoutPeople,
    [switch]$ResearchOnly,
    [switch]$Start,
    [string]$Instance,
    [int]$DatabasePort,
    [int]$WebPort,
    [int]$PeoplePort,
    [string]$BackupDirectory,
    [switch]$BackupPasswordFromStdin,
    [string]$PasswordCheckRuntime,
    [ValidateSet('', 'in-place', 'copy')][string]$Media = '',
    [string]$MediaFolder,
    [switch]$AllowUnprotectedMedia,
    [switch]$OldLibraryStopped,
    [string]$PgDump,
    [long]$DatabaseBudgetBytes,
    [switch]$Offline,
    [switch]$CatalogOnlyUpgrade,
    [switch]$NoDialog,
    [switch]$ProgressLines
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'setup-common.ps1')
$root = [IO.Path]::GetFullPath((Split-Path (Split-Path $PSScriptRoot -Parent) -Parent))
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
$configPath = Join-Path $root 'config\local.json'
$journalPath = Join-Path $root 'data\setup\install.json'
$shell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$steps = @(Get-SetupSteps)

# The backup password first, before any native child can start and inherit stdin unread.
$backupPasswordFrame = $null
if ($BackupPasswordFromStdin) {
    if ($PasswordFromStdin) { throw '-BackupPasswordFromStdin and -PasswordFromStdin are different messages: give one. Nothing was changed.' }
    if ($Mode -notin @('Install', 'Restore', 'Upgrade')) { throw '-BackupPasswordFromStdin is for -Mode Restore and catalog upgrades. Nothing was changed.' }
    . (Join-Path $PSScriptRoot 'setup-password.ps1')
    $backupPasswordFrame = Read-SetupPasswordFrame
}

if ($Mode -eq 'Upgrade') {
    if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Application Python is missing; no changes made.' }
    $upgradeArgs = @((Join-Path $root 'installer/upgrade-catalog.py'), '--config-root', (Join-Path $root 'config'), '--data-root', (Join-Path $root 'data'))
    if ($Offline -or $CatalogOnlyUpgrade) {
        if (-not ($Offline -and $CatalogOnlyUpgrade -and $BackupDirectory -and $PgDump -and $DatabaseBudgetBytes -gt 0)) {
            throw 'Apply requires -Offline -CatalogOnlyUpgrade -BackupDirectory -PgDump -DatabaseBudgetBytes. No backup bypass is supported.'
        }
        # The new catalog backup is protected by the administrator password, checked against its stored hash.
        $upgradeArgs += @('--mode', 'apply', '--offline', '--catalog-only-upgrade', '--backup-directory', $BackupDirectory, '--pg-dump', $PgDump, '--database-budget-bytes', [string]$DatabaseBudgetBytes)
        if ($backupPasswordFrame) {
            $code = Invoke-SetupPasswordChild $pythonExe ($upgradeArgs + '--backup-password-frame-stdin') $backupPasswordFrame
            if ($code -ne 0) { throw "Setup command failed (exit $code). See the output above; no later steps were run." }
            return
        }
    }
    Invoke-SetupCommand $pythonExe $upgradeArgs
    return
}

function Test-SetupPortInUse([int]$Port) { [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) }

if ($Mode -eq 'Plan') {
    $layout = Resolve-SetupLayout $Instance $DatabasePort $WebPort $PeoplePort
    Write-Output "FamilyFoto Core setup plan: $root"
    for ($i = 0; $i -lt $steps.Count; $i++) { Write-Output ("{0}. {1}" -f ($i + 1), $steps[$i]) }
    Write-Output 'Requires Windows x64, internet/cached installers and one administrator elevation. Installs private Python 3.12 unless -Python is supplied.'
    Write-Output 'Input: administrator password (twice, hidden). Select media source/destination later in Imports.'
    Write-Output "Application and database location is detected from the package root: $root"
    if ($layout.Names.Instance) {
        Write-Output "Second installation '$($layout.Names.Instance)': database service $($layout.Names.DatabaseService) on port $($layout.Database), web port $($layout.Web), People port $($layout.People), task '$($layout.Names.ServerTask)', shortcut '$($layout.Names.Shortcut)'."
        Write-Output 'Uses PostgreSQL''s plain program files, not its installer, so the other installation and its PostgreSQL record are not touched.'
    } else {
        Write-Output 'Creates private config/temp/setup directories, a PostgreSQL service, schema, local dependencies and desktop launcher.'
    }
    Write-Output 'Core alone: no People/Docker, LAN access, TLS trust, power changes, auto-start, imports or destructive cleanup.'
    if ($WithPeople) { Write-Output 'After Core: native People installation; requires -ResearchOnly. No Docker or scans.' }
    elseif ($WithoutPeople) { Write-Output 'People will not be installed (-WithoutPeople). It can be installed later with install-people.ps1.' }
    else { Write-Output 'Install asks whether to install People and shows the face model terms first. Unattended installs choose with -WithPeople -ResearchOnly or -WithoutPeople.' }
    Write-Output 'Install asks whether this is a new library or a restore from a backup. -Mode Restore restores a backup into this installation in one go.'
    if (Test-Path -LiteralPath $configPath) { Write-Output 'Existing configuration detected: Install will refuse. Verify is safe; Resume requires this installer journal.' }
    if (Test-Path -LiteralPath $journalPath) { Write-Output 'Setup journal exists. Use Resume for an interrupted fresh installation; Verify for a completed one.' }
    return
}
if ($Mode -eq 'Verify') {
    if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Application Python is missing; no changes made.' }
    Invoke-SetupCommand $pythonExe @((Join-Path $root 'installer/verify-installation.py'))
    if ($WithPeople) { Invoke-SetupCommand $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'install-people.ps1'), '-Mode', 'Verify') }
    return
}
# Flag mistakes are refused before anything is checked or changed; the question itself comes later.
if ($WithPeople -and -not $ResearchOnly) { throw 'Native People requires explicit -ResearchOnly model terms approval. No installation started.' }
if ($WithPeople -and $WithoutPeople) { throw 'Choose either -WithPeople or -WithoutPeople, not both. No installation started.' }
$restoreFlags = $BackupDirectory -or $Media -or $MediaFolder -or $AllowUnprotectedMedia -or $OldLibraryStopped -or $BackupPasswordFromStdin -or $PasswordCheckRuntime
if ($restoreFlags -and $Mode -notin @('Install', 'Restore')) { throw 'The backup, media and switch options are for a restore: use -Mode Restore.' }
if ($Mode -eq 'Install' -and $restoreFlags) { $Mode = 'Restore' }
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -lt 1) {
    throw 'Use Windows PowerShell 5.1 (powershell.exe) for Install/Resume/Restore. Plan and Verify also work in PowerShell 7.'
}
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) { throw 'Windows x64 PowerShell is required.' }
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or [int](Get-CimInstance Win32_OperatingSystem).BuildNumber -lt 22000) {
    throw 'This installer targets Windows 11 x64; other platforms are not qualified.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as administrator using the same Windows account that will operate FamilyFoto. Preview is available without elevation.'
}
if ($root.StartsWith('\\') -or $root -match '[\s"\r\n]') { throw 'The first installer supports a local application path without whitespace or quotes, for example C:\FamilyFoto.' }
foreach ($path in @($root, $configPath, $journalPath, "$root\temp", "$root\runtime", "$root\data", "$root\.venv", "$root\logs")) {
    Assert-SetupPath $path
}
$interactive = [Environment]::UserInteractive -and -not $PasswordFromStdin -and -not [Console]::IsInputRedirected
$fingerprint = Get-SetupFingerprint $root
$owner = $identity.User.Value
$existingJournal = if (Test-Path -LiteralPath $journalPath) { Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json } else { $null }
# A fresh, interactive setup asks everything in one window first (installer/windows/README.md, "The setup window"), then runs
# this script again unattended with the answers, and shows its progress and the result.
if ($interactive -and -not $NoDialog -and $Mode -in @('Install', 'Restore') -and -not $restoreFlags -and -not $existingJournal -and
    -not $Instance -and -not (Test-Path -LiteralPath $configPath)) {
    & (Join-Path $PSScriptRoot 'setup-dialog.ps1') -Root $root -Restore:($Mode -eq 'Restore')
    return
}
if ($ProgressLines) { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) }
if ($existingJournal) {
    # The installation's names and ports were chosen when it began; flags may repeat them, never change them.
    $recorded = if ($existingJournal.PSObject.Properties.Name -contains 'layout' -and $existingJournal.layout) { $existingJournal.layout } else { $null }
    $recordedInstance = if ($recorded) { [string]$recorded.instance } else { '' }
    if ($Instance -and $Instance -ne $recordedInstance) { throw "This installation began as '$recordedInstance'. Run it with the same -Instance, or without it." }
    $Instance = $recordedInstance
    if ($recorded -and $recordedInstance) {
        foreach ($pair in @(@($DatabasePort, $recorded.database, 'DatabasePort'), @($WebPort, $recorded.web, 'WebPort'), @($PeoplePort, $recorded.people, 'PeoplePort'))) {
            if ($pair[0] -and $pair[0] -ne [int]$pair[1]) { throw "This installation began with -$($pair[2]) $($pair[1]). Run it with the same port, or without it." }
        }
        $DatabasePort, $WebPort, $PeoplePort = [int]$recorded.database, [int]$recorded.web, [int]$recorded.people
    }
}
$layout = Resolve-SetupLayout $Instance $DatabasePort $WebPort $PeoplePort
$names = $layout.Names

# A completed Core installation goes straight to the restore when asked; nothing else changes it.
$coreComplete = $existingJournal -and $existingJournal.complete
if ($Mode -eq 'Restore' -and $coreComplete) {
    if ($existingJournal.root -ne $root -or $existingJournal.owner -ne $owner) { throw 'This installation was set up from another folder or Windows account. No changes made.' }
} else {
    if ($Mode -eq 'Restore' -and $existingJournal -and -not $coreComplete) { $Mode = 'Resume'; $restoring = $true }
    elseif ($Mode -eq 'Restore') { $Mode = 'Install'; $restoring = $true }
    else { $restoring = $false }
    $volume = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($root))
    if (-not $volume.IsReady -or $volume.DriveFormat -ne 'NTFS' -or $volume.AvailableFreeSpace -lt 10GB) {
        throw 'Choose a local NTFS application disk with at least 10 GB free (media space is additional).'
    }
    if ((Get-ScheduledTask -TaskName $names.ServerTask -ErrorAction SilentlyContinue) -or (Test-SetupPortInUse $layout.Web)) {
        throw "A FamilyFoto task '$($names.ServerTask)' or a listener on web port $($layout.Web) already exists. This installer cannot modify a running/existing app; use Verify, or install a second one with -Instance."
    }
    if ($Mode -eq 'Install') {
        if ($existingJournal) { throw 'Setup already started here; use Resume (or -Mode Restore for a restore).' }
        if ((Test-Path -LiteralPath $configPath) -or (Test-Path -LiteralPath "$root\data\postgres") -or
            (Get-Service $names.DatabaseService -ErrorAction SilentlyContinue) -or (Test-SetupPortInUse $layout.Database)) {
            throw "Existing configuration, cluster, database service $($names.DatabaseService) or occupied database port $($layout.Database). Fresh setup refused; nothing overwritten."
        }
        if ($names.Instance -and (Test-SetupPortInUse $layout.People)) { throw "People port $($layout.People) is in use. Choose another -PeoplePort." }
        if (-not $names.Instance -and (Test-Path -LiteralPath $PostgresRegistration)) {
            throw 'PostgreSQL 17 is already installed on this computer, by another FamilyFoto or another program. Install this one as a second installation: add -Instance NAME -DatabasePort -WebPort -PeoplePort. Nothing was changed.'
        }
        foreach ($existing in @('.venv', 'runtime\python', 'runtime\postgres', 'runtime\ffmpeg')) {
            if (Test-Path -LiteralPath (Join-Path $root $existing)) { throw 'Existing runtime found. This fresh installer will not adopt or overwrite it.' }
        }
        if (-not $restoring -and $interactive) {
            Write-Host ''
            Write-Host 'Is this a new library, or a restore from a backup?'
            Write-Host '  1. New library: start empty, and import photos later.'
            Write-Host '  2. Restore from a backup: bring a library from another computer, in one go.'
            $choice = ''
            for ($attempt = 0; $attempt -lt 3 -and $choice -notin @('1', '2'); $attempt++) { $choice = ([string](Read-Host 'Type 1 or 2')).Trim() }
            if ($choice -notin @('1', '2')) { throw 'No answer. Run the installer again and type 1 or 2. No installation started.' }
            $restoring = $choice -eq '2'
        }
        $people = Resolve-SetupPeopleChoice $WithPeople.IsPresent $WithoutPeople.IsPresent $ResearchOnly.IsPresent $null $interactive ${function:Read-SetupPeopleAnswer}
        $journal = [pscustomobject]@{version=1;root=$root;owner=$owner;fingerprint=$fingerprint;
            completed=@();active=$null;complete=$false;configHash=$null;people=$people;
            layout=[pscustomobject]@{instance=[string]$names.Instance;database=$layout.Database;web=$layout.Web;people=$layout.People};
            restore=$restoring}
    } else {
        if (-not $existingJournal) { throw 'No installer journal found. Existing/manual installations cannot be adopted automatically.' }
        $journal = $existingJournal
        Assert-SetupJournal $journal $root $owner $fingerprint
        if (-not $restoring -and $journal.PSObject.Properties.Name -contains 'restore' -and $journal.restore) { $restoring = $true }
        $people = Resolve-SetupPeopleChoice $WithPeople.IsPresent $WithoutPeople.IsPresent $ResearchOnly.IsPresent ([string]$journal.people) $interactive ${function:Read-SetupPeopleAnswer}
        if ($journal.configHash -and (Get-FileHash -LiteralPath $configPath).Hash -ne $journal.configHash) { throw 'Configuration changed after setup. Review before resuming.' }
        if ($journal.active -eq 'postgres-install') { throw 'PostgreSQL installation was interrupted. Inspect its service/cluster and installer logs; automatic reinstall is unsafe.' }
        if ($journal.active -eq 'ffmpeg' -and (Test-Path -LiteralPath "$root\runtime\ffmpeg\bin")) { throw 'FFmpeg installation was interrupted after creating tools. Verify the partial installation before resuming.' }
        if ($journal.completed -contains 'postgres-install') { Assert-SetupDatabaseService $root $names.DatabaseService }
        if ($journal.completed -contains 'schema') { Invoke-SetupCommand $pythonExe @("$root\installer\verify-installation.py", '--database-only', '--require-empty') }
    }

    # A fresh restore checks its backup password before Core is installed, so a wrong one changes nothing.
    if ($restoring -and $backupPasswordFrame -and $BackupDirectory) {
        $ownRuntime = -not $PasswordCheckRuntime
        if ($ownRuntime) {
            Write-Output 'Preparing password validation (a temporary private Python, removed afterwards)...'
            $runtime = New-SetupPasswordRuntime $root
        } else { $runtime = $PasswordCheckRuntime }
        try {
            Write-Output 'Checking backup password...'
            $answer = Invoke-SetupPasswordCheck $root $runtime $BackupDirectory $backupPasswordFrame
        } finally { if ($ownRuntime) { Remove-SetupPasswordRuntime $root $runtime } }
        if (-not $answer.ok) { throw "Restore stopped: $($answer.message) Nothing was installed." }
        Write-Output 'The backup password opens the backup.'
    }

    # Only fresh/owned setup directories are secured. No recursive disk ACL changes.
    Set-SetupPrivateDirectory "$root\data\setup"
    $lock = $null
    try {
        Assert-SetupPath "$root\data\setup\install.lock"
        $lock = [IO.File]::Open("$root\data\setup\install.lock", 'OpenOrCreate', 'ReadWrite', 'None')
        if ($Mode -eq 'Install' -and (Test-Path -LiteralPath $journalPath)) { throw 'Another setup instance created the journal; rerun with Resume.' }
        if ($Mode -eq 'Resume') {
            $latest = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
            if (($latest | ConvertTo-Json -Depth 6) -ne ($journal | ConvertTo-Json -Depth 6)) { throw 'Setup state changed concurrently. Rerun Resume.' }
            # A journal from before the People question gets the answer just given.
            if (-not ($journal.PSObject.Properties.Name -contains 'people') -or -not $journal.people) { $journal | Add-Member -NotePropertyName people -NotePropertyValue $people -Force }
            if (-not ($journal.PSObject.Properties.Name -contains 'layout')) {
                $journal | Add-Member -NotePropertyName layout -NotePropertyValue ([pscustomobject]@{instance='';database=$layout.Database;web=$layout.Web;people=$layout.People})
            }
            if ($restoring -and -not ($journal.PSObject.Properties.Name -contains 'restore' -and $journal.restore)) { $journal | Add-Member -NotePropertyName restore -NotePropertyValue $true -Force }
        }
        Save-SetupJournal $journalPath $journal
        Set-SetupPrivateDirectory "$root\config"
        Set-SetupPrivateDirectory "$root\temp"
        New-Item -ItemType Directory -Path "$root\logs" -Force | Out-Null
        Push-Location $root
        try {
            foreach ($step in $steps) {
                if (@($journal.completed) -contains $step) { Write-Output "Already completed: $step"; continue }
                $journal.active = $step
                Save-SetupJournal $journalPath $journal
                Write-Progress -Activity 'Installing FamilyFoto Core' -Status $step -PercentComplete (100 * @($journal.completed).Count / $steps.Count)
                Write-Output "Installing: $step"
                switch ($step) {
                    'python' {
                        $candidate = $Python
                        $prefix = @()
                        if (-not $candidate) {
                            Invoke-SetupCommand $shell @('-NoProfile', '-File', 'installer/windows/install-python.ps1')
                            $candidate = Join-Path $root 'runtime\python\python.exe'
                        }
                        Invoke-SetupCommand $candidate ($prefix + @('-c', 'import sys,struct; sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize(chr(80))==8 else 1)'))
                        Invoke-SetupCommand $candidate ($prefix + @('-m', 'venv', '.venv'))
                    }
                    'dependencies' {
                        if ($env:FAMILYFOTO_PAYLOAD_RECEIPT) {
                            Invoke-SetupCommand $pythonExe @('-m', 'installer.common.install_dependencies', '--role', 'core')
                        } else {
                            Invoke-SetupCommand $pythonExe @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt')
                        }
                        Invoke-SetupCommand $pythonExe @('-m', 'pip', 'check')
                    }
                    'configuration' {
                        if (Test-Path -LiteralPath $configPath) { throw 'Configuration already exists without a completed checkpoint. Review it; it will not be overwritten.' }
                        $configArgs = @('installer/configure.py', '--database-port', [string]$layout.Database, '--web-port', [string]$layout.Web)
                        if ($names.Instance) { $configArgs += @('--instance', $names.Instance) }
                        if ($restoring) {
                            # A restored library brings its own accounts, so nobody needs this password:
                            # a random one, never shown, as the Mac restore does. Nothing is asked.
                            $bytes = New-Object byte[] 32
                            $random = [Security.Cryptography.RandomNumberGenerator]::Create()
                            try { $random.GetBytes($bytes) } finally { $random.Dispose() }
                            [Convert]::ToBase64String($bytes) | & $pythonExe @($configArgs + '--password-stdin')
                            if ($LASTEXITCODE -ne 0) { throw "Setup command failed (exit $LASTEXITCODE). See the output above; no later steps were run." }
                        } else {
                            if ($PasswordFromStdin) { $configArgs += '--password-stdin' }
                            Invoke-SetupCommand $pythonExe $configArgs
                        }
                        $journal.configHash = (Get-FileHash -LiteralPath $configPath).Hash
                    }
                    'postgres-download' {
                        if ($names.Instance) {
                            Get-SetupDownload $PostgresBinariesUrl "$root\temp\postgresql-17.11-5-windows-x64-binaries.zip" $PostgresBinariesSha256
                        } else {
                            Get-SetupDownload $PostgresInstallerUrl "$root\temp\postgresql-17.11-5-windows-x64.exe" $PostgresInstallerSha256
                        }
                    }
                    'postgres-install' {
                        Invoke-SetupCommand $shell @('-NoProfile', '-File', 'installer/windows/install-postgres.ps1')
                        Assert-SetupDatabaseService $root $names.DatabaseService
                    }
                    'database' {
                        Assert-SetupDatabaseService $root $names.DatabaseService
                        Invoke-SetupCommand $pythonExe @('installer/init-database.py')
                        Restart-Service $names.DatabaseService
                        Invoke-SetupCommand $pythonExe @('installer/verify-installation.py', '--database-only', '--before-schema')
                    }
                    'schema' { Invoke-SetupCommand $pythonExe @('-m', 'app.database.schema_upgrading') }
                    'ffmpeg' { Invoke-SetupCommand $shell @('-NoProfile', '-File', 'installer/windows/install-ffmpeg.ps1') }
                    'verification' { Invoke-SetupCommand $pythonExe @('installer/verify-installation.py', '--require-empty') }
                    'launcher' {
                        $shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) "$($names.Shortcut).lnk"
                        if ((Test-Path -LiteralPath $shortcut) -or (Get-ScheduledTask -TaskName $names.ServerTask -ErrorAction SilentlyContinue)) {
                            throw 'Desktop shortcut/task already exists. It will not be overwritten; review partial launcher setup.'
                        }
                        Invoke-SetupCommand $shell @('-NoProfile', '-File', 'installer/windows/install-desktop-launcher.ps1')
                    }
                }
                $journal.completed = @($journal.completed) + @($step)
                $journal.active = $null
                Save-SetupJournal $journalPath $journal
            }
            $journal.complete = $true
            Save-SetupJournal $journalPath $journal
            Write-Progress -Activity 'Installing FamilyFoto Core' -Completed
            Write-Output 'Core installation complete. Remote/LAN access remains unconfigured.'
            if (-not $restoring) {
                if ($people -eq 'skip') { Write-Output 'People was not installed, as chosen. Install it later with installer/windows/install-people.ps1.' }
                if ($people -eq 'install') {
                    Write-Output 'Installing optional native People. Core is complete; if this step fails, inspect it separately rather than repeating Core setup.'
                    Invoke-SetupCommand $shell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'install-people.ps1'), '-Mode', 'Install', '-ResearchOnly', '-Port', [string]$layout.People)
                }
                Write-Output "Use $($names.Shortcut) on the desktop. Local URL: https://127.0.0.1:$($layout.Web)/ (install config\tls\ca.pem as a trusted root on each browser computer once)"
                if ($Start) { Start-ScheduledTask -TaskName $names.ServerTask }
            }
        } finally { Pop-Location }
    } finally {
        if ($lock) { $lock.Dispose() }
    }
    if (-not $restoring) { return }
    $existingJournal = $journal
}

# The restore, into this installation's own database and folders (installer/windows/restore.py).
# People is set up by the restore itself when chosen, so that it can check the restored faces.
$people = if ($existingJournal.PSObject.Properties.Name -contains 'people') { [string]$existingJournal.people } else { '' }
if (-not $people) { $people = Resolve-SetupPeopleChoice $WithPeople.IsPresent $WithoutPeople.IsPresent $ResearchOnly.IsPresent $null $interactive ${function:Read-SetupPeopleAnswer} }
$restoreArgs = @((Join-Path $root 'installer/windows/restore.py'), '--people-port', [string]$layout.People)
if ($people) { $restoreArgs += @('--people', $people) }
if ($BackupDirectory) { $restoreArgs += @('--backup', $BackupDirectory) }
if ($Media) { $restoreArgs += @('--media', $Media) }
if ($MediaFolder) { $restoreArgs += @('--media-folder', $MediaFolder) }
if ($AllowUnprotectedMedia) { $restoreArgs += '--allow-unprotected-media' }
if ($OldLibraryStopped) { $restoreArgs += '--old-library-stopped' }
if (-not $interactive) { $restoreArgs += '--unattended' }
if ($ProgressLines) { $restoreArgs += '--progress-lines' }
Push-Location $root
try {
    if ($backupPasswordFrame) {
        # Its own private pipe: restore.py opens the backup again itself before it writes anything.
        $code = Invoke-SetupPasswordChild $pythonExe ($restoreArgs + '--backup-password-frame-stdin') $backupPasswordFrame
    } else {
        & $pythonExe @restoreArgs
        $code = $LASTEXITCODE
    }
} finally {
    Pop-Location
    if ($backupPasswordFrame) { [Array]::Clear($backupPasswordFrame, 0, $backupPasswordFrame.Length) }
}
if ($code -eq 2) { Write-Output 'The library is restored and checked, but not switched on. Run -Mode Restore again to answer the switch warning.'; exit 2 }
if ($code -ne 0) { throw 'The restore stopped; the message above says why. Nothing was deleted. Run -Mode Restore again to carry on from where it stopped.' }
