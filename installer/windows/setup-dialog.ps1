<#
The setup dialog (installer/windows/README.md, "The setup window"): every question first, each answer checked before anything is
installed; then the install and restore run underneath with those answers, while this window shows
their progress; then the result, with a button that opens FamilyFoto.

Opened by install.ps1 for an interactive fresh setup. It runs install.ps1 again, unattended, with the
answers as its existing options, and the password on that process's standard input only.

A restore asks for the FamilyFoto administrator password from the old computer, as it was when the
backup was made: it opens the backup, and signs in afterwards (docs/BACKUP.md, "Backup password").
To check it before Start, the window prepares a temporary private Python (setup-password.ps1) as soon
as Restore is chosen, and removes it when the window closes. That is the one thing made before Start.

-RenderTo FOLDER draws each page, with sample answers, to PNG files there and shows nothing (for review
and tests). -BuildOnly builds the window and exits.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [switch]$Restore,
    [switch]$BuildOnly,
    [string]$RenderTo
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'setup-common.ps1')
. (Join-Path $PSScriptRoot 'setup-dialog-checks.ps1')
. (Join-Path $PSScriptRoot 'setup-password.ps1')
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

if (-not ('FamilyFotoSetupRun' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'setup-run.cs') }

$check = [char]0x2713; $arrow = [char]0x25B6; $circle = [char]0x25CB; $cross = [char]0x2715
$red = [Drawing.Color]::FromArgb(176, 40, 30); $green = [Drawing.Color]::FromArgb(30, 110, 60); $muted = [Drawing.Color]::FromArgb(90, 98, 94)
$font = New-Object Drawing.Font('Segoe UI', 9)
$bold = New-Object Drawing.Font('Segoe UI', 9, [Drawing.FontStyle]::Bold)
$heading = New-Object Drawing.Font('Segoe UI', 13, [Drawing.FontStyle]::Bold)

$form = New-Object Windows.Forms.Form
$form.Text = 'FamilyFoto setup'
$form.Font = $font
$form.ClientSize = New-Object Drawing.Size(760, 720)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$iconPath = Join-Path $Root 'web\familyfoto.ico'
if (Test-Path -LiteralPath $iconPath) { $form.Icon = New-Object Drawing.Icon($iconPath) }

function New-Page { $page = New-Object Windows.Forms.Panel; $page.SetBounds(0, 0, 760, 660); $page.AutoScroll = $true; $form.Controls.Add($page); $page }
function Add-Text($parent, [string]$text, [int]$x, [int]$y, [int]$width, [int]$height = 20, $font = $null, $color = $null) {
    $label = New-Object Windows.Forms.Label; $label.Text = $text; $label.SetBounds($x, $y, $width, $height)
    if ($font) { $label.Font = $font }; if ($color) { $label.ForeColor = $color }
    $parent.Controls.Add($label); $label
}
function Add-Box($parent, [int]$x, [int]$y, [int]$width) { $box = New-Object Windows.Forms.TextBox; $box.SetBounds($x, $y, $width, 24); $parent.Controls.Add($box); $box }
function Add-Button($parent, [string]$text, [int]$x, [int]$y, [int]$width = 90) { $button = New-Object Windows.Forms.Button; $button.Text = $text; $button.SetBounds($x, $y, $width, 28); $parent.Controls.Add($button); $button }
function Add-Choice($parent, [string]$text, [int]$x, [int]$y, [int]$width, [string]$kind = 'check') {
    $choice = if ($kind -eq 'radio') { New-Object Windows.Forms.RadioButton } else { New-Object Windows.Forms.CheckBox }
    $choice.Text = $text; $choice.SetBounds($x, $y, $width, 22); $parent.Controls.Add($choice); $choice
}
function Set-Message($label, [string]$problem, [string]$ok = '', [bool]$answered = $true) {
    # A question not answered yet stays quiet: "Still to do" at the bottom lists it.
    if (-not $answered) { $label.Text = ''; return }
    if ($problem) { $label.Text = $problem; $label.ForeColor = $red } else { $label.Text = $ok; $label.ForeColor = $green }
}

# Page 1: the questions.
$ask = New-Page
Add-Text $ask 'Set up FamilyFoto' 24 16 600 30 $heading | Out-Null
Add-Text $ask 'Answer these, and each answer is checked straight away. Nothing is installed or changed until you choose Start.' 24 48 700 20 $null $muted | Out-Null
$newLibrary = Add-Choice $ask 'New library: start empty, and import photos later' 24 76 460 'radio'
$restoreLibrary = Add-Choice $ask 'Restore from a backup: bring a library from another computer, in one go' 24 98 600 'radio'

$restoreGroup = New-Object Windows.Forms.GroupBox; $restoreGroup.Text = 'The backup'; $restoreGroup.SetBounds(24, 126, 700, 300); $ask.Controls.Add($restoreGroup)
Add-Text $restoreGroup 'Backup folder (the one named familyfoto-backup-..., holding "catalog")' 12 22 600 | Out-Null
$backupBox = Add-Box $restoreGroup 12 42 580; $backupBrowse = Add-Button $restoreGroup 'Browse...' 598 40
$backupMessage = Add-Text $restoreGroup '' 12 68 680 18
Add-Text $restoreGroup 'FamilyFoto administrator password from the old computer, as it was when this backup was made' 12 90 680 | Out-Null
$backupPassword = Add-Box $restoreGroup 12 110 380; $backupPassword.UseSystemPasswordChar = $true
$showBackupPassword = Add-Choice $restoreGroup 'Show' 400 111 70
$checkPassword = Add-Button $restoreGroup 'Check password' 476 108 130
$passwordCheckMessage = Add-Text $restoreGroup '' 12 136 680 34
$inPlace = Add-Choice $restoreGroup 'In place: use the copy on the backup disk. Fast and needs no other space, but that disk becomes the library disk, and this backup is used up.' 12 172 680 'radio'
$inPlace.Height = 34
$copy = Add-Choice $restoreGroup 'Copy: copy the photos and videos to a folder you choose. Slower, but the backup stays as it is.' 12 206 680 'radio'
$copy.Checked = $true
Add-Text $restoreGroup 'Where to make the library folder (a "FFLibrary" folder is made inside it)' 12 232 600 | Out-Null
$mediaBox = Add-Box $restoreGroup 12 252 580; $mediaBrowse = Add-Button $restoreGroup 'Browse...' 598 250
$mediaMessage = Add-Text $restoreGroup '' 12 278 680 18
$unprotected = Add-Choice $ask 'This disk keeps no file permissions (such as exFAT): I accept that anyone with the disk can read the photos and videos. The catalog stays encrypted.' 36 430 690
$unprotected.Height = 34; $unprotected.Visible = $false

$peopleGroup = New-Object Windows.Forms.GroupBox; $peopleGroup.Text = 'People (faces)'; $peopleGroup.SetBounds(24, 468, 700, 128); $ask.Controls.Add($peopleGroup)
$installPeople = Add-Choice $peopleGroup 'Install People: find faces in your photos so you can name the people in them' 12 20 660
$terms = New-Object Windows.Forms.TextBox; $terms.Multiline = $true; $terms.ReadOnly = $true; $terms.ScrollBars = 'Vertical'
$terms.Text = $PeopleTermsText.Replace("`n", "`r`n"); $terms.SetBounds(12, 44, 676, 52); $peopleGroup.Controls.Add($terms)
$acceptTerms = Add-Choice $peopleGroup 'I accept these terms (non-commercial research use only)' 12 100 500

$passwordLabel = Add-Text $ask 'Administrator password (at least 12 characters), twice: you sign in as "family" with it' 24 604 600
$password = Add-Box $ask 24 624 300; $password.UseSystemPasswordChar = $true
$repeat = Add-Box $ask 334 624 300; $repeat.UseSystemPasswordChar = $true
$passwordMessage = Add-Text $ask '' 24 650 700 34
$oldStopped = Add-Choice $ask 'FamilyFoto on the old computer is stopped for good: this restored library becomes the one you use.' 24 686 700
$advancedLink = New-Object Windows.Forms.LinkLabel; $advancedLink.Text = 'Advanced: installation name and ports'; $advancedLink.SetBounds(24, 714, 400, 20); $ask.Controls.Add($advancedLink)
$advanced = New-Object Windows.Forms.Panel; $advanced.SetBounds(24, 736, 700, 80); $advanced.Visible = $false; $ask.Controls.Add($advanced)
Add-Text $advanced 'Name (empty for the first FamilyFoto on this computer)' 0 0 330 | Out-Null
$instanceBox = Add-Box $advanced 0 20 200
Add-Text $advanced 'Database port' 220 0 100 | Out-Null; $databasePort = Add-Box $advanced 220 20 90
Add-Text $advanced 'Web port' 330 0 100 | Out-Null; $webPort = Add-Box $advanced 330 20 90
Add-Text $advanced 'People port' 440 0 100 | Out-Null; $peoplePort = Add-Box $advanced 440 20 90
$layoutMessage = Add-Text $advanced '' 0 48 700 30
$advancedLink.Add_LinkClicked({ $advanced.Visible = -not $advanced.Visible })

$askBar = New-Object Windows.Forms.Panel; $askBar.SetBounds(0, 660, 760, 60); $form.Controls.Add($askBar)
$startButton = Add-Button $askBar 'Start' 560 14 90; $startButton.Enabled = $false
$cancelButton = Add-Button $askBar 'Cancel' 656 14 80
$startHint = Add-Text $askBar '' 24 18 520 24 $null $muted

# Page 2: progress.
$run = New-Page; $run.Visible = $false
$runTitle = Add-Text $run 'Setting up FamilyFoto' 24 16 600 30 $heading
Add-Text $run 'This runs on its own. Keep this computer on, and the backup disk connected, until it says it has finished.' 24 48 700 20 $null $muted | Out-Null
$stageNames = [ordered]@{
    install = 'Installing FamilyFoto (Python, the database, video tools)'
    restored = 'Restoring the catalog and the photos and videos'
    checked = 'Checking the restored library against the backup'
    switched = 'Switching on the restored library'
    thumbnails = 'Putting back thumbnails, posters and face pictures'
    installed = 'Moving the library into this installation'
    'restore-access' = 'Checking desktop access to restored files'
    people = 'Setting up People'
    started = 'Starting FamilyFoto'
    repaired = 'Rebuilding any missing thumbnail or poster'
}
$stageLabels = @{}
$y = 82
foreach ($name in $stageNames.Keys) { $stageLabels[$name] = Add-Text $run "$circle  $($stageNames[$name])" 32 $y 680 22; $y += 26 }
$stepLabel = Add-Text $run '' 32 $y 680 20 $null $muted
$bar = New-Object Windows.Forms.ProgressBar; $bar.SetBounds(32, ($y + 26), 690, 18); $run.Controls.Add($bar)
$countLabel = Add-Text $run '' 32 ($y + 48) 690 20 $null $muted
$showDetails = Add-Choice $run 'Show details' 32 ($y + 76) 200
$details = New-Object Windows.Forms.TextBox; $details.Multiline = $true; $details.ReadOnly = $true; $details.ScrollBars = 'Both'; $details.WordWrap = $false
$details.Font = New-Object Drawing.Font('Consolas', 8.5); $details.SetBounds(32, ($y + 100), 690, 200); $details.Visible = $false; $run.Controls.Add($details)
$showDetails.Add_CheckedChanged({ $details.Visible = $showDetails.Checked })

# Page 3: the result.
$done = New-Page; $done.Visible = $false
$resultTitle = Add-Text $done '' 24 16 700 30 $heading
$resultText = Add-Text $done '' 24 56 700 160
$openButton = Add-Button $done 'Open FamilyFoto' 24 230 150
$trustButton = Add-Button $done "Trust this computer's certificate" 184 230 230
$retryButton = Add-Button $done 'Try again' 24 230 120
$switchButton = Add-Button $done 'Switch on' 24 230 120
$closeButton = Add-Button $done 'Close' 640 230 90
$trustText = Add-Text $done 'Only needed if the browser warns about the connection: adds this FamilyFoto''s certificate authority to your account''s trusted certificates. Windows asks you to confirm.' 24 266 700 40 $null $muted

# The answers.
$state = @{ DiskProblem = $null; LayoutKey = $null; LayoutResult = $null; MediaBytes = $null;
    PasswordRuntime = $null; Preparer = $null; PrepareProblem = $null; Checker = $null; CheckingFor = $null; CheckedFor = $null; CheckResult = $null; Folder = $null; Layout = $null; Running = $false; Runner = $null; Arguments = $null; Url = $null; Authority = $null; LogPath = $null }

function Get-Layout {
    # Port and service lookups are slow, so they run only when the name or a port changes, not on every keystroke.
    $name = $instanceBox.Text.Trim()
    $ports = foreach ($box in @($databasePort, $webPort, $peoplePort)) { $value = 0; [void][int]::TryParse($box.Text.Trim(), [ref]$value); $value }
    $cacheKey = "$name|$($ports -join '|')"
    if ($state.LayoutKey -eq $cacheKey) { return $state.LayoutResult }
    $state.LayoutKey = $cacheKey
    $state.LayoutResult = Measure-Layout $name $ports
    $state.LayoutResult
}

function Measure-Layout([string]$name, $ports) {
    try { $layout = Resolve-SetupLayout $name $ports[0] $ports[1] $ports[2] } catch { return @{ Problem = $_.Exception.Message } }
    foreach ($port in @($layout.Database, $layout.Web, $layout.People)) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { return @{ Problem = "Port $port is in use. Choose another, under Advanced." } }
    }
    if (Get-Service $layout.Names.DatabaseService -ErrorAction SilentlyContinue) { return @{ Problem = "$($layout.Names.DatabaseService) already exists. Choose another name, under Advanced." } }
    if (-not $name -and (Test-Path -LiteralPath $PostgresRegistration)) { return @{ Problem = 'This computer already has a FamilyFoto (PostgreSQL 17 is installed). Give this one a name and its own ports, under Advanced.' } }
    $layout
}

# The password check (setup-password.ps1): prepared once, in the background, when Restore is chosen.
function Start-PasswordPreparation {
    if ($RenderTo -or $BuildOnly -or $state.PasswordRuntime -or $state.Preparer) { return }
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null
    $state.PrepareProblem = $null
    $state.Preparer = New-Object FamilyFotoSetupRun
    $state.Preparer.Start("$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe",
        [string[]]@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'setup-password.ps1'), '-PrepareFor', $Root),
        $Root, $null, (Join-Path $Root ('logs\setup-password-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')))
    $passwordTimer.Start()
}

function Read-PasswordWork {
    if ($state.Preparer) {
        $line = $null; $lines = @()
        while ($state.Preparer.Lines.TryDequeue([ref]$line)) { $lines += $line }
        foreach ($text in $lines) {
            if ($text -match '^@@ (\{.*\})\s*$') { try { $state.PasswordRuntime = [string]($Matches[1] | ConvertFrom-Json).password_runtime } catch { } }
            elseif ($text -match '^! ' -and $text -notmatch '^! (At |\+ |\s|CategoryInfo|FullyQualifiedErrorId)') { $state.PrepareProblem = $text.Substring(2) }
        }
        if ($state.Preparer.Finished) {
            if ($state.Preparer.ExitCode -ne 0 -or -not $state.PasswordRuntime) {
                $state.PasswordRuntime = $null
                if (-not $state.PrepareProblem) { $state.PrepareProblem = 'The password check could not be prepared.' }
            } else { $state.PrepareProblem = $null }
            $state.Preparer.Close(); $state.Preparer = $null
            Update-Answers
        }
    }
    if ($state.Checker) {
        $line = $null
        while ($state.Checker.Lines.TryDequeue([ref]$line)) { if ($line -match '^\{') { try { $state.CheckAnswer = $line | ConvertFrom-Json } catch { } } }
        if ($state.Checker.Finished) {
            $answer = $state.CheckAnswer
            if (-not $answer) { $answer = [pscustomobject]@{ ok = $false; message = 'The password check gave no answer.' } }
            # A result for an earlier backup or password is thrown away.
            if ($state.CheckingFor -eq (Get-PasswordPair)) { $state.CheckedFor = $state.CheckingFor; $state.CheckResult = $answer }
            $state.Checker.Close(); $state.Checker = $null; $state.CheckingFor = $null; $state.CheckAnswer = $null
            Update-Answers
        }
    }
    if (-not $state.Preparer -and -not $state.Checker) { $passwordTimer.Stop() }
}

function Get-PasswordPair { "$($backupBox.Text.Trim())`n$($backupPassword.Text)" }

function Start-PasswordCheck {
    if ($state.PrepareProblem -and -not $state.Preparer) { Start-PasswordPreparation; Update-Answers; return }
    $pair = Get-PasswordPair
    if (-not $state.PasswordRuntime -or $state.Checker -or -not $backupPassword.Text -or $state.CheckedFor -eq $pair) { return }
    if (Test-SetupBackupFolder $backupBox.Text.Trim()) { return }
    # One check at a time, and only when asked or on leaving the field: never on every keystroke.
    $state.CheckingFor = $pair; $state.CheckAnswer = $null
    $state.Checker = New-Object FamilyFotoSetupRun
    $state.Checker.StartFramed((Join-Path $state.PasswordRuntime 'python\python.exe'),
        [string[]]@('-I', '-B', (Join-Path $Root 'installer\password_check.py'), '--site', (Join-Path $state.PasswordRuntime 'site'), '--backup', $backupBox.Text.Trim().TrimEnd('\')),
        $Root, [FamilyFotoSetupRun]::Frame($backupPassword.Text), (Join-Path $Root 'logs\setup-password-check.log'))
    $passwordTimer.Start()
    Update-Answers
}

function Update-Answers {
    $restoring = $restoreLibrary.Checked
    if ($restoring) { Start-PasswordPreparation }
    $restoreGroup.Enabled = $restoring; $oldStopped.Visible = $restoring
    $problems = @()
    if ($restoring) {
        $backupProblem = Test-SetupBackupFolder $backupBox.Text.Trim()
        Set-Message $backupMessage $backupProblem "$check A finished FamilyFoto backup." ([bool]$backupBox.Text.Trim())
        $checking = [bool]$state.Checker
        $checkPassword.Enabled = -not $checking -and [bool]$backupPassword.Text -and ([bool]$state.PasswordRuntime -or ([bool]$state.PrepareProblem -and -not $state.Preparer))
        $checkPassword.Text = if ($state.PrepareProblem -and -not $state.Preparer) { 'Try again' } else { 'Check password' }
        if ($backupProblem) { $problems += 'the backup folder'; $passwordCheckMessage.Text = ''; $mediaMessage.Text = ''; $unprotected.Visible = $false }
        else {
            $state.MediaBytes = Get-SetupBackupMediaBytes $backupBox.Text.Trim()
            $pair = Get-PasswordPair
            if ($state.CheckedFor -eq $pair -and $state.CheckResult.ok) {
                Set-Message $passwordCheckMessage $null "$check This password opens the backup. After the restore, the administrator signs in with it."
            } else {
                $problems += 'the backup password'
                $passwordCheckMessage.ForeColor = $muted
                $passwordCheckMessage.Text = if ($state.PrepareProblem -and -not $state.Preparer) { "Preparing the password check failed: $($state.PrepareProblem) Choose Try again." }
                    elseif (-not $state.PasswordRuntime) { 'Preparing password validation... (a temporary private Python, removed when this window closes)' }
                    elseif ($checking) { 'Checking backup password...' }
                    elseif ($state.CheckedFor -eq $pair) { $null }
                    elseif ($backupPassword.Text) { 'Choose Check password.' } else { '' }
                if ($state.CheckedFor -eq $pair -and -not $state.CheckResult.ok) { Set-Message $passwordCheckMessage ([string]$state.CheckResult.message) }
                if ($state.PrepareProblem -and -not $state.Preparer) { $passwordCheckMessage.ForeColor = $red }
            }
            $media = if ($inPlace.Checked) { 'in-place' } else { 'copy' }
            $folder = Test-SetupMediaFolder $backupBox.Text.Trim() $media $mediaBox.Text.Trim() $state.MediaBytes
            $state.Folder = $folder.Folder
            Set-Message $mediaMessage $folder.Problem "$check The library folder will be $($folder.Folder)" ([bool]$mediaBox.Text.Trim())
            $unprotected.Visible = -not $folder.Problem -and $folder.Unprotected
            if ($folder.Problem) { $problems += 'the library folder' }
            elseif ($folder.Unprotected -and -not $unprotected.Checked) { $problems += 'accepting a disk without file permissions' }
        }
        if (-not $oldStopped.Checked) { $problems += 'confirming the old FamilyFoto is stopped' }
    }
    if ($installPeople.Checked -and -not $acceptTerms.Checked) { $problems += 'accepting the People terms' }
    $acceptTerms.Enabled = $installPeople.Checked
    # A restored library brings its own accounts, so a restore asks no password (installer/windows/README.md, "The setup window").
    foreach ($control in @($passwordLabel, $password, $repeat, $passwordMessage)) { $control.Visible = -not $restoring }
    $top = if ($restoring) { 604 } else { 686 }  # Close the gap the password leaves.
    $oldStopped.Top = $top + $ask.AutoScrollPosition.Y; $advancedLink.Top = $top + 28 + $ask.AutoScrollPosition.Y; $advanced.Top = $top + 50 + $ask.AutoScrollPosition.Y
    if (-not $restoring) {
        $passwordProblem = Test-SetupPassword $password.Text $repeat.Text
        Set-Message $passwordMessage $passwordProblem "$check Sign in to FamilyFoto as ""family"" with this password." ([bool]($password.Text -or $repeat.Text))
        if ($passwordProblem) { $problems += 'the password' }
    }
    $state.DiskProblem = Test-SetupInstallationDisk $Root
    if ($state.DiskProblem) { $problems += 'room for the installation' }
    $layout = Get-Layout
    if ($layout.ContainsKey('Problem')) { Set-Message $layoutMessage $layout.Problem; $advanced.Visible = $true; $problems += 'the installation name and ports'; $state.Layout = $null }
    else {
        $state.Layout = $layout
        $where = if ($layout.Names.Instance) { "A second FamilyFoto on this computer, named '$($layout.Names.Instance)'." } else { 'The first FamilyFoto on this computer.' }
        Set-Message $layoutMessage $null "$check $where Web address https://127.0.0.1:$($layout.Web)/"
    }
    $startButton.Enabled = $problems.Count -eq 0
    $startHint.Text = if ($state.DiskProblem) { $state.DiskProblem } elseif ($problems.Count) { 'Still to do: ' + ($problems -join ', ') + '.' } else { 'Ready.' }
    $startHint.ForeColor = if ($state.DiskProblem) { $red } else { $muted }
}

$suggested = Find-SetupLayout
$instanceBox.Text = $suggested.Instance; $databasePort.Text = $suggested.Database; $webPort.Text = $suggested.Web; $peoplePort.Text = $suggested.People
if ($env:FAMILYFOTO_SETUP_BACKUP) {
    $restoreLibrary.Checked = $true
    $backupBox.Text = $env:FAMILYFOTO_SETUP_BACKUP
}
if ($env:FAMILYFOTO_PAYLOAD_RECEIPT) {
    $prepared = Get-Content -LiteralPath $env:FAMILYFOTO_PAYLOAD_RECEIPT -Raw | ConvertFrom-Json
    $selectedPeople = @($prepared.features) -contains 'people'
    $installPeople.Checked = $selectedPeople
    $installPeople.Enabled = $false
    # The shared launcher already collected one combined agreement for this selection.
    $acceptTerms.Checked = $selectedPeople
    $acceptTerms.Visible = $false
}
if ($Restore) { $restoreLibrary.Checked = $true } else { $newLibrary.Checked = $true }
foreach ($control in @($backupBox, $backupPassword, $mediaBox, $password, $repeat, $instanceBox, $databasePort, $webPort, $peoplePort)) { $control.Add_TextChanged({ Update-Answers }) }
foreach ($control in @($newLibrary, $restoreLibrary, $inPlace, $copy, $unprotected, $installPeople, $acceptTerms, $oldStopped)) { $control.Add_CheckedChanged({ Update-Answers }) }

$backupBrowse.Add_Click({
    $picker = New-Object Windows.Forms.FolderBrowserDialog; $picker.Description = 'Choose the FamilyFoto backup folder (familyfoto-backup-...)'
    if ($picker.ShowDialog($form) -eq 'OK') { $backupBox.Text = $picker.SelectedPath }
})
$showBackupPassword.Add_CheckedChanged({ $backupPassword.UseSystemPasswordChar = -not $showBackupPassword.Checked })
$checkPassword.Add_Click({ Start-PasswordCheck })
$backupPassword.Add_Leave({ Start-PasswordCheck })
$passwordTimer = New-Object Windows.Forms.Timer; $passwordTimer.Interval = 200
$passwordTimer.Add_Tick({ Read-PasswordWork })
$mediaBrowse.Add_Click({
    $picker = New-Object Windows.Forms.FolderBrowserDialog; $picker.Description = 'Choose where to make the library folder'; $picker.ShowNewFolderButton = $true
    if ($picker.ShowDialog($form) -eq 'OK') { $mediaBox.Text = $picker.SelectedPath }
})

# Running.
function Set-Stage([string]$name, [string]$how) {
    if (-not $stageLabels.Contains($name)) { return }
    $mark = switch ($how) { 'running' { $arrow } 'done' { $check } 'failed' { $cross } default { $circle } }
    $stageLabels[$name].Text = "$mark  $($stageNames[$name])"
    $stageLabels[$name].Font = if ($how -eq 'running') { $bold } else { $font }
    $stageLabels[$name].ForeColor = switch ($how) { 'done' { $green } 'failed' { $red } default { [Drawing.SystemColors]::ControlText } }
}

function Start-Setup {
    $layout = $state.Layout
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'install.ps1'),
        '-ProgressLines', '-NoDialog')
    if ($layout.Names.Instance) { $arguments += @('-Instance', $layout.Names.Instance, '-DatabasePort', [string]$layout.Database, '-WebPort', [string]$layout.Web, '-PeoplePort', [string]$layout.People) }
    if ($installPeople.Checked) { $arguments += @('-WithPeople', '-ResearchOnly') } else { $arguments += '-WithoutPeople' }
    if ($restoreLibrary.Checked) {
        # The password goes on the process's stdin as one frame (Invoke-Run), never in these arguments.
        $arguments += @('-Mode', 'Restore', '-BackupDirectory', $backupBox.Text.Trim(), '-BackupPasswordFromStdin',
            '-PasswordCheckRuntime', $state.PasswordRuntime,
            '-Media', $(if ($inPlace.Checked) { 'in-place' } else { 'copy' }), '-MediaFolder', $state.Folder, '-OldLibraryStopped')
        if ($unprotected.Visible -and $unprotected.Checked) { $arguments += '-AllowUnprotectedMedia' }
    } else {
        $arguments += @('-Mode', 'Install', '-Start', '-PasswordFromStdin')
        foreach ($name in @('restored', 'checked', 'switched', 'thumbnails', 'installed', 'repaired')) { $stageLabels[$name].Visible = $false }
        if (-not $installPeople.Checked) { $stageLabels['people'].Visible = $false }
    }
    $state.Arguments = $arguments
    $state.Url = "https://127.0.0.1:$($layout.Web)/"
    $state.Authority = Join-Path $Root 'config\tls\ca.pem'
    $runTitle.Text = if ($restoreLibrary.Checked) { 'Restoring FamilyFoto' } else { 'Setting up FamilyFoto' }
    $ask.Visible = $false; $askBar.Visible = $false; $run.Visible = $true
    Invoke-Run
}

function Invoke-Run {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'logs') | Out-Null
    $state.LogPath = Join-Path $Root ('logs\setup-dialog-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    $state.Runner = New-Object FamilyFotoSetupRun
    $state.Running = $true
    $shellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    if ($restoreLibrary.Checked) {
        # The administrator password the backup was made with, as one frame. Kept in its box until this
        # window closes or the restore finishes, so Try again can send it again.
        $state.Runner.StartFramed($shellPath, [string[]]$state.Arguments, $Root, [FamilyFotoSetupRun]::Frame($backupPassword.Text), $state.LogPath)
    } else {
        $state.Runner.Start($shellPath, [string[]]$state.Arguments, $Root, $password.Text, $state.LogPath)  # A new library's account password.
    }
    $timer.Start()
}

function Read-Lines {
    $line = $null
    while ($state.Runner.Lines.TryDequeue([ref]$line)) {
        $details.AppendText($line + "`r`n")
        $parsed = ConvertFrom-SetupProgressLine $line
        switch ($parsed.Kind) {
            'core-step' { Set-Stage 'install' 'running'; $stepLabel.Text = "Installing: $($parsed.Step)" }
            'core-done' { Set-Stage 'install' 'done'; $stepLabel.Text = '' }
            'stage' {
                if ($parsed.State -eq 'running') { Set-Stage 'install' 'done'; Set-Stage $parsed.Stage 'running'; $bar.Style = 'Marquee'; $countLabel.Text = '' }
                else { Set-Stage $parsed.Stage 'done' }
            }
            'count' {
                $e = $parsed.Event; $names = @($e.PSObject.Properties.Name)
                if ($names -contains 'total_bytes' -and $e.total_bytes -gt 0) {
                    $bar.Style = 'Continuous'; $bar.Value = [Math]::Min(100, [int](100 * $e.bytes / $e.total_bytes))
                    $phase = if ($names -contains 'phase') { [string]$e.phase } else { '' }
                    $verb = switch ($phase) { 'verify' { 'Checking' } 'copy' { 'Copying' } default { 'Working on' } }
                    $countLabel.Text = ('{0} {1:N0} of {2:N0} files, {3:N1} of {4:N1} GB' -f $verb, $e.files, $e.total_files, ($e.bytes / 1GB), ($e.total_bytes / 1GB))
                } elseif ($names -contains 'files') { $countLabel.Text = ('Reading the library: {0:N0} files' -f $e.files) }
            }
            default { if ($line -match '^Installing optional native People') { Set-Stage 'install' 'done'; Set-Stage 'people' 'running' } }
        }
    }
}

function Show-Result([int]$code) {
    $state.Running = $false; $state.Runner.Close()
    $run.Visible = $false; $done.Visible = $true
    $openButton.Visible = $trustButton.Visible = $trustText.Visible = $code -eq 0
    $retryButton.Visible = $code -notin @(0, 2)
    $switchButton.Visible = $code -eq 2
    if ($code -eq 2) {
        # Shouldn't happen through this window, which requires the switch box, but is handled.
        $resultTitle.Text = 'Restored, not switched on yet'; $resultTitle.ForeColor = [Drawing.SystemColors]::ControlText
        $resultText.Text = "The library is restored and checked against the backup, but not switched on.`r`n`r`nSwitch on makes it the library you use: first stop FamilyFoto on the old computer for good. Close leaves it for later; running the installer again shows the warning."
        return
    }
    if ($code -eq 0) {
        $resultTitle.Text = "$check FamilyFoto is ready"; $resultTitle.ForeColor = $green
        $report = Join-Path $Root 'restore\MOVE-IN-REPORT.json'
        if ($restoreLibrary.Checked -and (Test-Path -LiteralPath $report)) {
            $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
            if ($result.url) { $state.Url = [string]$result.url + '/' }
            if ($result.certificate_authority) { $state.Authority = [string]$result.certificate_authority }
            $people = if ($result.people -is [string] -or -not $result.people) { 'People is not installed: the saved people can be viewed; install People later to edit them.' }
                elseif ($result.people.library_check.passed) { 'People is set up, and can be edited.' }
                else { 'People is installed, but its check of the restored faces did not pass, so they can be viewed, not edited.' }
            $backupPassword.Text = ''
            $resultText.Text = ("FamilyFoto is running with the restored library: {0:N0} photos and videos.`r`n`r`nSign in with the old library's accounts. The administrator's password is the one that opened the backup.`r`n`r`n{1}`r`n`r`nKeep the backup until a new backup of this library has been made and verified.`r`n`r`nAddress: {2}" -f [long]$result.media_files, $people, $state.Url)
        } else {
            $resultText.Text = "FamilyFoto is running. Sign in as ""family"" with the password you chose.`r`n`r`nIt can take a few seconds to answer the first time.`r`n`r`nAddress: $($state.Url)"
        }
    } else {
        $resultTitle.Text = "$cross Setup stopped"; $resultTitle.ForeColor = $red
        $reason = @($details.Lines | Where-Object { $_ -match '^! (Restore stopped|Setup command failed|.*refused|.*Refused)' -or $_ -match '^Restore stopped' } | Select-Object -Last 1)
        if (-not $reason) { $reason = @($details.Lines | Where-Object { $_ -match '^! ' -and $_ -notmatch '^! (At |\+ |\s)' } | Select-Object -First 1) }
        $why = if ($reason) { ([string]$reason[0]).TrimStart('!', ' ') } else { "It ended with code $code." }
        $resultText.Text = "$why`r`n`r`nNothing was deleted. Try again carries on from where it stopped, with the same answers. The full output is in $($state.LogPath)."
        foreach ($name in $stageLabels.Keys) { if ($stageLabels[$name].Font.Bold) { Set-Stage $name 'failed' } }
    }
}

$timer = New-Object Windows.Forms.Timer; $timer.Interval = 250
$timer.Add_Tick({
    Read-Lines
    if ($state.Runner.Finished) { Read-Lines; $timer.Stop(); Show-Result $state.Runner.ExitCode }
})
$startButton.Add_Click({ Start-Setup })
$cancelButton.Add_Click({ $form.Close() })
$closeButton.Add_Click({ $form.Close() })
$openButton.Add_Click({ Start-Process $state.Url })
$trustButton.Add_Click({
    try { Import-Certificate -FilePath $state.Authority -CertStoreLocation Cert:\CurrentUser\Root | Out-Null; $trustText.Text = "$check Trusted. Reload the page in the browser." }
    catch { $trustText.Text = "Not trusted: $($_.Exception.Message)" }
})
$switchButton.Add_Click({
    if ($state.Arguments -notcontains '-OldLibraryStopped') { $state.Arguments += '-OldLibraryStopped' }
    $done.Visible = $false; $run.Visible = $true; $details.Clear()
    Invoke-Run
})
$retryButton.Add_Click({
    $done.Visible = $false; $run.Visible = $true; $details.Clear()
    Invoke-Run
})
$form.Add_FormClosing({
    param($sender, $closing)
    if ($state.Running) {
        $closing.Cancel = $true
        [Windows.Forms.MessageBox]::Show($form, 'FamilyFoto is still being set up. Wait until this window says it has finished or stopped; running the installer again then carries on.', 'FamilyFoto setup') | Out-Null
        return
    }
    # The password check's temporary Python goes with the window. A crash leaves it for the next setup to remove.
    $passwordTimer.Stop()
    foreach ($worker in @($state.Checker, $state.Preparer)) { if ($worker) { $worker.Stop(); $worker.Close() } }
    $backupPassword.Text = ''; $password.Text = ''; $repeat.Text = ''
    try { Remove-SetupPasswordRuntime $Root $state.PasswordRuntime } catch { }
})

Update-Answers

if ($RenderTo) {
    # Sample answers, to draw each page for review. Nothing is run or changed.
    New-Item -ItemType Directory -Force -Path $RenderTo | Out-Null
    # Controls only paint once shown, so the window is shown off-screen while it is drawn.
    $form.StartPosition = 'Manual'; $form.Location = New-Object Drawing.Point(-3000, -3000); $form.ShowInTaskbar = $false
    $form.Show(); [Windows.Forms.Application]::DoEvents()
    $save = {
        param($name)
        [Windows.Forms.Application]::DoEvents()
        $image = New-Object Drawing.Bitmap($form.Width, $form.Height)
        $form.DrawToBitmap($image, (New-Object Drawing.Rectangle(0, 0, $image.Width, $image.Height)))
        $image.Save((Join-Path $RenderTo "$name.png")); $image.Dispose()
    }
    & $save 'page1-questions'
    $ask.AutoScrollPosition = New-Object Drawing.Point(0, 400); & $save 'page1-questions-lower'
    $ask.Visible = $false; $askBar.Visible = $false; $run.Visible = $true
    Set-Stage 'install' 'done'; Set-Stage 'restored' 'running'; $bar.Value = 34; $countLabel.Text = 'Copying 4,210 of 12,686 files, 2.8 of 8.3 GB'
    $showDetails.Checked = $true; $details.Text = "Installing: python`r`n...`r`nCore installation complete.`r`n@@ {""stage"": ""restored"", ""state"": ""running""}"
    & $save 'page2-progress'
    $run.Visible = $false; $done.Visible = $true
    $resultTitle.Text = "$check FamilyFoto is ready"; $resultTitle.ForeColor = $green
    $resultText.Text = "FamilyFoto is running with the restored library: 12,686 photos and videos.`r`n`r`nSign in with the old library's accounts. The administrator's password is the one that opened the backup.`r`n`r`nPeople is set up, and can be edited.`r`n`r`nKeep the backup until a new backup of this library has been made and verified.`r`n`r`nAddress: https://127.0.0.1:4342/"
    $retryButton.Visible = $false; $switchButton.Visible = $false
    & $save 'page3-done'
    $resultTitle.Text = "$cross Setup stopped"; $resultTitle.ForeColor = $red; $openButton.Visible = $trustButton.Visible = $trustText.Visible = $false; $retryButton.Visible = $true
    $resultText.Text = "Restore stopped: The password is incorrect, or the backup's password information is damaged.`r`n`r`nNothing was deleted. Try again carries on from where it stopped, with the same answers."
    & $save 'page3-stopped'
    $form.Hide(); $form.Dispose()
    return
}
if ($BuildOnly) { return }
[void]$form.ShowDialog()
