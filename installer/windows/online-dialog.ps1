[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Agreement, [Parameter(Mandatory=$true)][string]$Choices, [string]$RenderTo)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object Windows.Forms.Form
$form.Text = 'FamilyFoto installation'
$form.ClientSize = New-Object Drawing.Size(720, 600)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.Font = New-Object Drawing.Font('Segoe UI', 9)
$label = New-Object Windows.Forms.Label
$label.Text = 'Installation folder'
$label.SetBounds(20, 18, 650, 24)
$folder = New-Object Windows.Forms.TextBox
$folder.Text = 'C:\FamilyFoto'
$folder.SetBounds(20, 45, 680, 26)
$people = New-Object Windows.Forms.CheckBox
$people.Text = 'Include People (model terms apply)'
$people.SetBounds(20, 85, 680, 26)
$terms = New-Object Windows.Forms.TextBox
$terms.Multiline = $true
$terms.ReadOnly = $true
$terms.ScrollBars = 'Vertical'
$terms.Text = (Get-Content -LiteralPath $Agreement -Raw -Encoding UTF8) -replace '\r?\n', "`r`n"
$terms.SetBounds(20, 125, 680, 350)
$accept = New-Object Windows.Forms.CheckBox
$accept.Text = 'I accept the FamilyFoto and applicable third-party terms above.'
$accept.SetBounds(20, 490, 680, 30)
$start = New-Object Windows.Forms.Button
$start.Text = 'Continue'
$start.Enabled = $false
$start.SetBounds(580, 540, 120, 34)
$accept.Add_CheckedChanged({ $start.Enabled = $accept.Checked })
$start.Add_Click({
    if (-not [IO.Path]::IsPathRooted($folder.Text) -or $folder.Text -match '[\s"\r\n]' -or $folder.Text.StartsWith('\\')) {
        [Windows.Forms.MessageBox]::Show('Choose a local absolute folder without spaces or quotes.'); return
    }
    @{ root=$folder.Text; people=$people.Checked; agreement_digest=(Get-FileHash -LiteralPath $Agreement).Hash.ToLowerInvariant() } |
        ConvertTo-Json | Set-Content -LiteralPath $Choices -Encoding UTF8
    $form.Close()
})
$form.Controls.AddRange(@($label,$folder,$people,$terms,$accept,$start))
if ($RenderTo) {
    $form.StartPosition = 'Manual'
    $form.Location = New-Object Drawing.Point(-3000, -3000)
    $form.ShowInTaskbar = $false
    $form.Show()
    [Windows.Forms.Application]::DoEvents()
    $bitmap = New-Object Drawing.Bitmap($form.Width, $form.Height)
    $form.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
    $bitmap.Save($RenderTo, [Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    $form.Hide()
    $form.Dispose()
    return
}
[void]$form.ShowDialog()
$form.Dispose()
