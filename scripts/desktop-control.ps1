$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'instance-names.ps1')
$config = Get-Content "$root\config\local.json" -Raw | ConvertFrom-Json
$names = Get-FamilyFotoConfigNames $config
$taskName = $names.ServerTask
$dataRoot = if ($config.live_existing_catalog) { $config.preview.data_root } else { "$root\data" }
$scheme = if ($config.tls) { 'https' } else { 'http' }
$address = if ($config.host -eq '0.0.0.0') { 'localhost' } else { $config.host }
$url = "${scheme}://${address}:$($config.port)/"
$form = New-Object Windows.Forms.Form
$form.Text = $names.Shortcut
$iconPath = Join-Path $root 'web\familyfoto.ico'
if (Test-Path -LiteralPath $iconPath) { $form.Icon = New-Object Drawing.Icon($iconPath) }
$form.ClientSize = New-Object Drawing.Size(480,195)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$status = New-Object Windows.Forms.Label
$status.SetBounds(20,20,440,30)
$form.Controls.Add($status)
$note = New-Object Windows.Forms.Label
$note.Text = "Closing this window leaves the server running.`r`nPause imports and conversions in FamilyFoto before stopping.`r`nStop Server does not terminate media workers."
$note.SetBounds(20,110,440,70)
$form.Controls.Add($note)
function Show-Failure($exception) {
    [Windows.Forms.MessageBox]::Show($exception.Message, 'FamilyFoto') | Out-Null
}
$start = New-Object Windows.Forms.Button
$start.Text = 'Start Server'
$start.SetBounds(20,60,135,34)
$start.Add_Click({
    try { Start-ScheduledTask -TaskName $taskName } catch { Show-Failure $_.Exception }
})
$form.Controls.Add($start)
$open = New-Object Windows.Forms.Button
$open.Text = 'Open FamilyFoto'
$open.SetBounds(170,60,135,34)
$open.Add_Click({ Start-Process $url })
$form.Controls.Add($open)
$stop = New-Object Windows.Forms.Button
$stop.Text = 'Stop Server'
$stop.SetBounds(320,60,135,34)
$stop.Add_Click({
    $answer = [Windows.Forms.MessageBox]::Show('Stop the web server? Pause media work in FamilyFoto first. This does not force-stop workers or PostgreSQL.', 'FamilyFoto', 'YesNo', 'Warning')
    if ($answer -eq 'Yes') {
        try { [IO.File]::WriteAllText("$dataRoot\DESKTOP-SERVER-STOP", 'Desktop stop request') } catch { Show-Failure $_.Exception }
    }
})
$form.Controls.Add($stop)
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({
    try {
        $task = Get-ScheduledTask -TaskName $taskName
        $status.Text = "Server task: $($task.State)"
        $start.Enabled = $task.State -ne 'Running'
        $stop.Enabled = $task.State -eq 'Running'
        $open.Enabled = $task.State -eq 'Running'
    } catch { $status.Text = 'Launcher is not installed.' }
})
$form.Add_Shown({ $timer.Start() })
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })
[void]$form.ShowDialog()
