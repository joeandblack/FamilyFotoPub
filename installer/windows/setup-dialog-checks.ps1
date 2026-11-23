# Checks for the setup dialog's answers, made before anything is installed (no Python yet).
# Each Test-* returns $null when the answer will do, or a sentence saying what to change.
# Dot-source setup-common.ps1 first.

function Test-SetupBackupFolder([string]$Path) {
    if (-not $Path) { return 'Choose the backup folder.' }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return "$Path is not a folder." }
    if (-not (Test-Path -LiteralPath (Join-Path $Path 'catalog') -PathType Container)) {
        return "$Path is not a FamilyFoto backup folder: it has no ""catalog"" folder. Choose the folder named familyfoto-backup-..."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Path 'library\complete.fernet') -PathType Leaf)) {
        return "$Path is not a finished backup: it has no sealed library index. Choose a backup that finished."
    }
    # A backup made with a recovery key file, before backups were protected by the administrator password.
    if (-not (Test-Path -LiteralPath (Join-Path $Path 'password.json') -PathType Leaf)) {
        return 'This backup format is not supported. Create a new backup with this version. (A backup made with a recovery key file restores only with the FamilyFoto release that made it.)'
    }
    $null
}

# The photos' size, for the free-space check, from the backup's timing record (not encrypted).
function Get-SetupBackupMediaBytes([string]$Path) {
    $timing = Join-Path $Path 'capture-timing.json'
    try { return [long]((Get-Content -LiteralPath $timing -Raw | ConvertFrom-Json).media_bytes) } catch { return $null }
}

# The backup's password is checked by the restore's own code, on a temporary private Python
# (setup-password.ps1, installer/password_check.py): Windows PowerShell's .NET has no scrypt, and no
# second implementation is written here.

# Where the restored photos go. Returns @{ Problem; Folder; Unprotected }: Problem is $null when the
# folder will do; Unprotected says the disk keeps no file permissions (exFAT), which needs a yes.
function Test-SetupMediaFolder([string]$Backup, [string]$Media, [string]$Parent, [Nullable[long]]$MediaBytes) {
    $result = @{ Problem = $null; Folder = $null; Unprotected = $false }
    if (-not $Parent) { $result.Problem = 'Choose where to make the library folder.'; return $result }
    if (-not (Test-Path -LiteralPath $Parent -PathType Container)) { $result.Problem = "$Parent is not a folder."; return $result }
    $parentFull = [IO.Path]::GetFullPath($Parent)
    $folder = Join-Path $parentFull 'FFLibrary'
    $result.Folder = $folder
    if (Test-Path -LiteralPath $folder) { $result.Problem = "$folder already exists. Choose another place: the restore makes a new library folder."; return $result }
    # The library folder is always new, so the backup can't be inside it; it only mustn't be inside the backup.
    $backupFull = [IO.Path]::GetFullPath($Backup).TrimEnd('\') + '\'
    if (($folder + '\').StartsWith($backupFull, [StringComparison]::OrdinalIgnoreCase)) {
        $result.Problem = 'The library folder must be outside the backup folder.'; return $result
    }
    $root = [IO.Path]::GetPathRoot($parentFull)
    if ($Media -eq 'in-place' -and $root -ne [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Backup))) {
        $result.Problem = "In place keeps the photos on the backup's disk, so the library folder must be on $([IO.Path]::GetPathRoot($Backup)) too. Choose a folder there, or choose Copy."
        return $result
    }
    $drive = New-Object IO.DriveInfo($root)
    $result.Unprotected = $drive.DriveFormat -notin @('NTFS', 'ReFS')
    if ($Media -eq 'copy' -and $MediaBytes -and $drive.AvailableFreeSpace -lt ($MediaBytes * 1.05 + 1GB)) {
        $result.Problem = ('{0} has {1:N1} GB free, and the copy needs about {2:N1} GB. Choose a folder on a disk with more room.' -f $root, ($drive.AvailableFreeSpace / 1GB), (($MediaBytes * 1.05 + 1GB) / 1GB))
    }
    $result
}

# The installation's own disk: install.ps1 needs a local NTFS disk with 10 GB free. The catalog and
# the library's data land here; the restore engine's own space check, knowing their real size, runs too.
function Test-SetupInstallationDisk([string]$Root) {
    $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Root)))
    if (-not $drive.IsReady -or $drive.DriveFormat -ne 'NTFS') { return "FamilyFoto installs on a local NTFS disk, and $($drive.Name) isn't one. Put the FamilyFoto folder on another disk." }
    if ($drive.AvailableFreeSpace -lt 10GB) { return ('{0} has {1:N1} GB free; the installation needs 10 GB. Free some space, or put the FamilyFoto folder on another disk.' -f $drive.Name, ($drive.AvailableFreeSpace / 1GB)) }
    $null
}

function Test-SetupPassword([string]$Password, [string]$Repeat) {
    if ($Password.Length -lt 12) { return 'The password needs at least 12 characters.' }
    if ($Password -cne $Repeat) { return 'The two passwords are not the same.' }
    $null
}

# The names and ports for this installation: the original ones on a computer with no FamilyFoto,
# otherwise a named second installation on the first free ports.
function Find-SetupLayout {
    $inUse = { param([int]$Port) [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) }
    $first = -not (Test-Path -LiteralPath $PostgresRegistration) -and -not (Get-Service 'FamilyFotoPostgres' -ErrorAction SilentlyContinue) -and
        -not (Get-ScheduledTask -TaskName 'FamilyFoto Desktop Server' -ErrorAction SilentlyContinue) -and
        -not (& $inUse 5432) -and -not (& $inUse 4340) -and -not (& $inUse 54341)
    if ($first) { return @{ Instance = ''; Database = 5432; Web = 4340; People = 54341 } }
    $name = 'restore'; $n = 1
    while (Get-Service "FamilyFotoPostgres-$name" -ErrorAction SilentlyContinue) { $n++; $name = "restore-$n" }
    $taken = @(Get-CimInstance Win32_Service -Filter "Name LIKE 'FamilyFotoPostgres%'" -ErrorAction SilentlyContinue | ForEach-Object { if ($_.PathName -match '-p\s+(\d+)') { [int]$Matches[1] } })
    $pick = { param([int]$Start, [int]$Stop) for ($p = $Start; $p -le $Stop; $p++) { if (-not (& $inUse $p) -and $p -notin $taken) { return $p } } }
    @{ Instance = $name; Database = (& $pick 5434 5499); Web = (& $pick 4342 4399); People = (& $pick 54344 54399) }
}

# One line of the installer's output, as the dialog's progress understands it.
function ConvertFrom-SetupProgressLine([string]$Line) {
    if ($Line -match '^@@ (\{.*\})\s*$') {
        try { $parsed = $Matches[1] | ConvertFrom-Json } catch { return @{ Kind = 'text'; Text = $Line } }
        if ($parsed.PSObject.Properties.Name -contains 'stage') { return @{ Kind = 'stage'; Stage = [string]$parsed.stage; State = [string]$parsed.state } }
        return @{ Kind = 'count'; Event = $parsed }
    }
    if ($Line -match '^Installing: (\S+)') { return @{ Kind = 'core-step'; Step = $Matches[1] } }
    if ($Line -match '^Already completed: (\S+)') { return @{ Kind = 'core-step'; Step = $Matches[1] } }
    if ($Line -match '^Core installation complete') { return @{ Kind = 'core-done' } }
    @{ Kind = 'text'; Text = $Line }
}
