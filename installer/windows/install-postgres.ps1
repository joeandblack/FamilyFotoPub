# Fresh-install PostgreSQL provisioning, for this installation's own service and port.
param([string]$Installer, [string]$Archive)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'setup-common.ps1')
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$configPath=Join-Path $root 'config\local.json'
$config=Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$names=Get-FamilyFotoConfigNames $config
$service=$names.DatabaseService
$port=[int]$config.database.port
$cluster=Join-Path $root 'data\postgres'
if (Get-Service $service -ErrorAction SilentlyContinue) { throw "$service already exists; do not reinstall over a cluster." }
if (Test-Path "$cluster\PG_VERSION") { throw 'Existing database cluster must be preserved.' }

if (-not $names.Instance) {
  # The original installation: PostgreSQL's own installer, as before.
  if (Test-Path -LiteralPath $PostgresRegistration) { throw 'PostgreSQL 17 is already installed on this computer, by FamilyFoto or another program. Install this FamilyFoto as a second installation: add -Instance NAME and its own ports.' }
  if (-not $Installer) { $Installer=Join-Path $root 'temp\postgresql-17.11-5-windows-x64.exe' }
  if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash -ne $PostgresInstallerSha256) { throw 'Installer checksum mismatch.' }
  $options=Join-Path $root 'temp\postgres-install.options'
  $lines=@('mode=unattended','unattendedmodeui=none',"prefix=$root\runtime\postgres","datadir=$cluster","serverport=$port","servicename=$service",'superaccount=postgres',"superpassword=$($config.postgres_admin_password)",'disable-components=pgAdmin,stackbuilder','create_shortcuts=0','locale=C','enable_acledit=1')
  try {
    [IO.File]::WriteAllLines($options,$lines)
    $p=Start-Process -FilePath $Installer -ArgumentList '--optionfile',$options -WindowStyle Hidden -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "PostgreSQL installation failed: $($p.ExitCode)" }
  } finally { if (Test-Path -LiteralPath $options) { Remove-Item -LiteralPath $options } }
  Get-Service $service
  return
}

# A second installation: the same release's plain program files, nothing registered but this
# installation's own service. The original installation and its registry record are not touched.
if (-not $Archive) { $Archive=Join-Path $root 'temp\postgresql-17.11-5-windows-x64-binaries.zip' }
if ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash -ne $PostgresBinariesSha256) { throw 'PostgreSQL archive checksum mismatch.' }
$programs=Join-Path $root 'runtime\postgres'
if (Test-Path -LiteralPath $programs) { throw 'PostgreSQL program files already exist here. Inspect them; they are not overwritten.' }
$extract=Join-Path $root 'temp\postgres-extract'
Assert-SetupPath $extract
if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
Write-Output 'Unpacking PostgreSQL program files.'
[IO.Compression.ZipFile]::ExtractToDirectory($Archive, $extract)
foreach ($extra in @('pgAdmin 4', 'StackBuilder', 'symbols', 'doc')) {
  $path=Join-Path $extract "pgsql\$extra"
  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Move-Item -LiteralPath (Join-Path $extract 'pgsql') -Destination $programs
Remove-Item -LiteralPath $extract -Recurse -Force
$bin=Join-Path $programs 'bin'

# The administrator password goes through a private file in this installation's temp folder,
# never the command line, and is removed straight after.
$passwordFile=Join-Path $root 'temp\postgres-admin.pw'
Assert-SetupPath $passwordFile
try {
  [IO.File]::WriteAllText($passwordFile, [string]$config.postgres_admin_password, (New-Object Text.UTF8Encoding($false)))
  Invoke-SetupCommand (Join-Path $bin 'initdb.exe') @('-D', $cluster, '-U', 'postgres', "--pwfile=$passwordFile", '-E', 'UTF8', '--locale=C', '-A', 'scram-sha-256')
} finally { if (Test-Path -LiteralPath $passwordFile) { Remove-Item -LiteralPath $passwordFile -Force } }
Add-Content -LiteralPath (Join-Path $cluster 'postgresql.conf') -Encoding ASCII -Value @(
  '', '# FamilyFoto second installation', "port = $port", "listen_addresses = '127.0.0.1'", 'logging_collector = on')

# The service runs as Network Service, as PostgreSQL's installer sets it up: it owns the cluster
# and can read the program files.
$networkService='*S-1-5-20'
Invoke-SetupCommand 'icacls.exe' @($cluster, '/grant', "${networkService}:(OI)(CI)F", '/T', '/Q')
Invoke-SetupCommand 'icacls.exe' @($programs, '/grant', "${networkService}:(OI)(CI)RX", '/T', '/Q')
Invoke-SetupCommand (Join-Path $bin 'pg_ctl.exe') @('register', '-N', $service, '-U', 'NT AUTHORITY\NetworkService', '-D', $cluster, '-S', 'auto', '-w')
Start-Service $service
Get-Service $service
