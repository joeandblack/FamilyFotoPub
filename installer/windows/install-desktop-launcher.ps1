# Register the desktop entry point; runtime scripts remain in scripts/.
# A second installation on the computer gets its own task and shortcut (scripts/instance-names.ps1).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $root 'scripts\instance-names.ps1')
$names = Get-FamilyFotoConfigNames (Get-Content -LiteralPath (Join-Path $root 'config\local.json') -Raw | ConvertFrom-Json)
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\desktop-server.ps1`"" -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if (Get-ScheduledTask -TaskName $names.ServerTask -ErrorAction SilentlyContinue) { throw "The task '$($names.ServerTask)' already exists. It is not replaced." }
Register-ScheduledTask -TaskName $names.ServerTask -Action $action -Principal $principal -Settings $settings -Description "Manual FamilyFoto web server for $root. No automatic startup or recovery." | Out-Null
$desktop = [Environment]::GetFolderPath('Desktop')
$link = Join-Path $desktop "$($names.Shortcut).lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\desktop-control.ps1`""
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'Start, open, or gracefully stop FamilyFoto'
$shortcut.IconLocation = "$root\web\familyfoto.ico,0"
$shortcut.Save()
Write-Output "Installed: $link"
