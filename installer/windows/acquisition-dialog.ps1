[CmdletBinding()]
param([string]$Python, [string]$Engine, [string]$Descriptor, [string]$Attempt, [string]$Bundle)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Path (Join-Path $PSScriptRoot 'setup-run.cs')
$runner = New-Object FamilyFotoSetupRun
$form = New-Object Windows.Forms.Form
$form.Text = 'FamilyFoto setup'
$form.ClientSize = New-Object Drawing.Size(720, 460)
$form.StartPosition = 'CenterScreen'
$status = New-Object Windows.Forms.Label
$status.Text = 'Preparing installation...'
$status.SetBounds(20, 20, 680, 32)
$bar = New-Object Windows.Forms.ProgressBar
$bar.Style = 'Marquee'
$bar.SetBounds(20, 60, 680, 20)
$log = New-Object Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = 'Vertical'
$log.SetBounds(20, 100, 680, 290)
$cancel = New-Object Windows.Forms.Button
$cancel.Text = 'Cancel'
$cancel.SetBounds(580, 410, 120, 30)
$cancelPath = Join-Path $Attempt 'cancel.requested'
$cancel.Add_Click({ [IO.File]::WriteAllText($cancelPath, 'cancel'); $cancel.Enabled = $false; $status.Text = 'Cancelling...' })
$form.Add_FormClosing({
    if (-not $runner.Finished) {
        $_.Cancel = $true
        [IO.File]::WriteAllText($cancelPath, 'cancel')
        $cancel.Enabled = $false
        $status.Text = 'Cancelling...'
    }
})
$form.Controls.AddRange(@($status,$bar,$log,$cancel))
$arguments = @('-E', '-s', '-B', '-m', 'installer.windows.online_setup', '--descriptor', $Descriptor, '--attempt', $Attempt, '--prepare-only')
if ($Bundle) { $arguments += @('--bundle', $Bundle) }
$runner.Start($Python, [string[]]$arguments, $Engine, '', (Join-Path $Attempt 'preparation.log'))
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 200
$timer.Add_Tick({
    $line = $null
    while ($runner.Lines.TryDequeue([ref]$line)) {
        if ($line.StartsWith('@@ ')) {
            try {
                $event = $line.Substring(3) | ConvertFrom-Json
                $status.Text = [string]$event.component_id
                if ($event.total -gt 0) {
                    $bar.Style = 'Continuous'
                    $bar.Value = [Math]::Min(100, [int](100.0 * $event.completed / $event.total))
                } else { $bar.Style = 'Marquee' }
            } catch { $log.AppendText($line + "`r`n") }
        } else { $log.AppendText($line + "`r`n") }
    }
    if ($runner.Finished) { $timer.Stop(); $form.Close() }
})
$timer.Start()
[void]$form.ShowDialog()
$exitCode = $runner.ExitCode
$runner.Close()
$timer.Dispose()
$form.Dispose()
exit $exitCode
