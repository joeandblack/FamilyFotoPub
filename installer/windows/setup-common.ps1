# Shared installer helpers. Loading this file performs no installation.
if ($PSVersionTable.PSVersion.Major -eq 5) {
    # A caller such as PowerShell 7 can leave incompatible module paths inherited.
    $env:PSModulePath = "$PSHOME\Modules;${env:ProgramFiles}\WindowsPowerShell\Modules"
}
Set-StrictMode -Version Latest
. (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts\instance-names.ps1')

# The original installation's ports. A named second installation chooses its own.
$SetupDefaultPorts = @{ Database = 5432; Web = 4340; People = 54341 }
# PostgreSQL's own installer records one PostgreSQL 17 installation per computer (in the registry,
# with an uninstall entry), so a second FamilyFoto never runs it: it uses the same release's plain
# program files instead, and registers its own service.
$PostgresInstallerUrl = 'https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64.exe'
$PostgresInstallerSha256 = '612C7F5400A003AAFFA0372CB273EED52278F93C680AB911EB9CB2229E329072'
$PostgresBinariesUrl = 'https://get.enterprisedb.com/postgresql/postgresql-17.11-5-windows-x64-binaries.zip'
$PostgresBinariesSha256 = '80379B2C04D51C30225532E0AE04509899141E9957ED096FE749D7FD9DF8F82F'
$PostgresRegistration = 'HKLM:\SOFTWARE\PostgreSQL\Installations\postgresql-x64-17'

# Which names and ports this installation uses. A named installation gives all three ports, so
# nothing about it is guessed; the original installation keeps the original ports.
function Resolve-SetupLayout([string]$Name, [int]$Database, [int]$Web, [int]$People) {
    $names = Get-FamilyFotoNames $Name
    if (-not $Name) {
        if (($Database -and $Database -ne $SetupDefaultPorts.Database) -or ($Web -and $Web -ne $SetupDefaultPorts.Web) -or ($People -and $People -ne $SetupDefaultPorts.People)) {
            throw 'Other ports are for a second installation on this computer: add -Instance NAME as well. No installation started.'
        }
        return @{ Names = $names; Database = $SetupDefaultPorts.Database; Web = $SetupDefaultPorts.Web; People = $SetupDefaultPorts.People }
    }
    if (-not ($Database -and $Web -and $People)) {
        throw 'A second installation needs its own ports: add -DatabasePort, -WebPort and -PeoplePort, for example -DatabasePort 5434 -WebPort 4342 -PeoplePort 54344. No installation started.'
    }
    foreach ($port in @($Database, $Web)) { if ($port -lt 1024 -or $port -gt 65535) { throw 'Use database and web ports between 1024 and 65535.' } }
    if ($People -lt 49152 -or $People -gt 65535) { throw 'Use a People port between 49152 and 65535.' }
    if (@(@($Database, $Web, $People) | Select-Object -Unique).Count -ne 3) { throw 'The three ports must differ.' }
    if (@(@($SetupDefaultPorts.Database, $SetupDefaultPorts.Web, $SetupDefaultPorts.People) | Where-Object { $_ -in @($Database, $Web, $People) }).Count) {
        throw 'The original installation uses ports 5432, 4340 and 54341. Choose others for a second installation.'
    }
    @{ Names = $names; Database = $Database; Web = $Web; People = $People }
}

function Assert-SetupPath([string]$Path) {
    $item = [IO.Path]::GetFullPath($Path)
    while ($item) {
        if (Test-Path -LiteralPath $item) {
            if ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Linked setup path is not allowed: $item"
            }
        }
        $parent = Split-Path $item -Parent
        if ($parent -eq $item) { break }
        $item = $parent
    }
}

function Set-SetupPrivateDirectory([string]$Path) {
    Assert-SetupPath $Path
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetOwner($owner)
    foreach ($sid in @($owner.Value, 'S-1-5-18', 'S-1-5-32-544')) {
        $identity = New-Object Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Invoke-SetupCommand([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Setup command failed (exit $LASTEXITCODE). See the output above; no later steps were run." }
}

function Get-SetupSteps {
    @('python', 'dependencies', 'configuration', 'postgres-download', 'postgres-install',
      'database', 'schema', 'ffmpeg', 'verification', 'launcher')
}

function Get-SetupFingerprint([string]$Root) {
    $paths = @('requirements.txt', 'installer/windows/install.ps1', 'installer/windows/setup-common.ps1', 'scripts/instance-names.ps1',
        'installer/configure.py', 'installer/windows/install-python.ps1', 'installer/windows/install-postgres.ps1', 'installer/init-database.py',
        'installer/windows/install-ffmpeg.ps1', 'installer/windows/install-desktop-launcher.ps1',
        'installer/verify-installation.py', 'app/database/schema_upgrading.py')
    $paths += @(Get-ChildItem -LiteralPath (Join-Path $Root 'databases') -Filter '*.sql' -File |
        Sort-Object Name | ForEach-Object { 'databases/' + $_.Name })
    $text = ($paths | ForEach-Object { $_ + ':' + (Get-FileHash -LiteralPath (Join-Path $Root $_) -Algorithm SHA256).Hash }) -join "`n"
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)))).Replace('-', '') }
    finally { $hash.Dispose() }
}

function Save-SetupJournal([string]$Path, $Journal) {
    Assert-SetupPath $Path
    Assert-SetupPath ($Path + '.new')
    [IO.File]::WriteAllText($Path + '.new', ($Journal | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $Path) {
        Assert-SetupPath ($Path + '.previous')
        [IO.File]::Replace($Path + '.new', $Path, $Path + '.previous')
    }
    else { [IO.File]::Move($Path + '.new', $Path) }
}

function Assert-SetupJournal($Journal, [string]$Root, [string]$Owner, [string]$Fingerprint) {
    if ($Journal.version -ne 1 -or $Journal.root -ne $Root -or $Journal.owner -ne $Owner) {
        throw 'Setup journal belongs to another folder, account or format. No changes made.'
    }
    if ($Journal.fingerprint -ne $Fingerprint) { throw 'Setup code changed since the interrupted install. Maintainer review is required before resuming.' }
    if ($Journal.complete) { throw 'Installation already completed. Use Verify; upgrades are not implemented by this script.' }
    $steps = @(Get-SetupSteps)
    $done = @($Journal.completed)
    if ($done.Count -gt $steps.Count) { throw 'Invalid setup checkpoint.' }
    for ($i = 0; $i -lt $done.Count; $i++) {
        if ($done[$i] -ne $steps[$i]) { throw 'Setup checkpoints are out of order.' }
    }
}

# -Network: a component the release deliberately does not ship (FFmpeg, from gyan.dev). With prepared
# payloads it still comes from its pinned URL when the receipt lacks it, and is checked against the
# same pin. Every other component keeps the rule: prepared payloads only, no network fallback.
function Get-SetupDownload([string]$Url, [string]$Path, [string]$Sha256, [switch]$Network) {
    # PowerShell 5's per-chunk progress rendering can dominate large downloads.
    $ProgressPreference = 'SilentlyContinue'
    Assert-SetupPath $Path
    if ($env:FAMILYFOTO_PAYLOAD_RECEIPT -and -not (Test-Path -LiteralPath $Path)) {
        Assert-SetupPath $env:FAMILYFOTO_PAYLOAD_RECEIPT
        $receipt = Get-Content -LiteralPath $env:FAMILYFOTO_PAYLOAD_RECEIPT -Raw | ConvertFrom-Json
        if ($receipt.schema_version -ne 1) { throw 'Unsupported installer payload receipt.' }
        $key = $Sha256.ToLowerInvariant()
        # Strict mode: read .Value only from a property that exists.
        $entry = $receipt.payloads.PSObject.Properties[$key]
        $source = if ($entry) { $entry.Value } else { $null }
        if ($source) {
            Assert-SetupPath $source
            if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $Sha256) {
                throw 'Prepared payload was modified. Nothing was installed.'
            }
            Copy-Item -LiteralPath $source -Destination $Path -ErrorAction Stop
        } elseif (-not $Network) {
            throw 'Required prepared payload is missing. Network fallback is disabled.'
        }
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        $partial = $Path + '.partial'
        Assert-SetupPath $partial
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing -TimeoutSec 600
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Sha256) {
            throw 'Downloaded artifact failed its checksum. It was not installed.'
        }
        Move-Item -LiteralPath $partial -Destination $Path
    }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Sha256) {
        throw 'Cached artifact failed its checksum. It was not installed.'
    }
}

# $Name, not $Service: PowerShell names ignore case, so a [string]$Service parameter would turn the
# $service CIM object below into text, and reading its PathName would fail under strict mode.
function Assert-SetupDatabaseService([string]$Root, [string]$Name = 'FamilyFotoPostgres') {
    $service = Get-CimInstance Win32_Service -Filter "Name='$Name'"
    $exe = Join-Path $Root 'runtime\postgres\bin\pg_ctl.exe'
    $cluster = Join-Path $Root 'data\postgres'
    if (-not $service -or $service.PathName -notmatch ('^"?' + [regex]::Escape($exe) + '"?\s') -or
        $service.PathName -notmatch ('\s-D\s+"?' + [regex]::Escape($cluster) + '"?(\s|$)')) {
        throw 'Database service does not belong to this installation. No service changes permitted.'
    }
    if ((Get-Content -LiteralPath (Join-Path $cluster 'PG_VERSION') -Raw).Trim() -ne '17') {
        throw 'Unexpected PostgreSQL major version; refusing to continue.'
    }
}

$PeopleTermsText = @'
People finds faces in your photos and groups them, so you can name the people in them.
It uses the InsightFace face model (buffalo_l), which its authors license for
non-commercial research use only. The model is downloaded during installation.
Terms: https://github.com/deepinsight/insightface/tree/master/python-package
'@

# Whether to install People: 'install' or 'skip'. There is no silent default. A flag decides it,
# or the setup journal of an interrupted install, or an interactive answer to the question.
function Resolve-SetupPeopleChoice([bool]$WithPeople, [bool]$WithoutPeople, [bool]$ResearchOnly,
                                   [string]$Recorded, [bool]$Interactive, [scriptblock]$Ask) {
    if ($WithPeople -and $WithoutPeople) { throw 'Choose either -WithPeople or -WithoutPeople, not both. No installation started.' }
    if ($ResearchOnly -and -not $WithPeople) { throw '-ResearchOnly accepts the face model terms, so it goes with -WithPeople. No installation started.' }
    if ($WithPeople -and -not $ResearchOnly) { throw 'Native People requires explicit -ResearchOnly model terms approval. No installation started.' }
    $flag = if ($WithPeople) { 'install' } elseif ($WithoutPeople) { 'skip' } else { $null }
    if ($Recorded) {
        if ($Recorded -notin @('install', 'skip')) { throw 'Invalid People choice in the setup journal. No changes made.' }
        if ($flag -and $flag -ne $Recorded) { throw "This installation started with People set to '$Recorded'. Resume with the same choice." }
        return $Recorded
    }
    if ($flag) { return $flag }
    if (-not $Interactive) {
        throw 'Choose whether to install People: add -WithPeople -ResearchOnly (accepting the face model terms) or -WithoutPeople. No installation started.'
    }
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        $answer = [string](& $Ask $PeopleTermsText)
        switch ($answer.Trim().ToLowerInvariant()) {
            { $_ -in @('yes', 'y') } { return 'install' }
            { $_ -in @('no', 'n') } { return 'skip' }
        }
    }
    throw 'No answer about People. Run the installer again and answer yes or no. No installation started.'
}

function Read-SetupPeopleAnswer([string]$Terms) {
    Write-Host ''
    Write-Host 'Install People (faces)?'
    Write-Host $Terms
    Read-Host 'Install People and accept these terms? Type yes or no'
}
