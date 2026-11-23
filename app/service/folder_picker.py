"""Interactive server-desktop folder selection; never starts an import."""
import os
import subprocess
import sys
import threading

_lock = threading.Lock()
SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if (-not [Environment]::UserInteractive -or (Get-Process -Id $PID).SessionId -eq 0) {
    throw 'The server must run in a signed-in Windows desktop session.'
}
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.Text = 'FamilyFoto folder selection'
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Size = New-Object System.Drawing.Size(420, 100)
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
$owner.MinimizeBox = $false
$owner.MaximizeBox = $false
$owner.TopMost = $true
$owner.ShowInTaskbar = $true
$picker = New-Object System.Windows.Forms.FolderBrowserDialog
$picker.Description = 'FamilyFoto: select an import source folder (selection does not start import)'
$picker.ShowNewFolderButton = $false
try {
    # A hidden owner does not reliably make the native dialog topmost.
    # Show and activate a real owner before creating its modal chooser.
    # The taskbar entry also lets the user recover focus if Windows denies it.
    $owner.Show()
    $owner.BringToFront()
    $owner.Activate()
    [System.Windows.Forms.Application]::DoEvents()
    if ($picker.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($picker.SelectedPath)
    }
} finally { $picker.Dispose(); $owner.Dispose() }
'''


MAC_SCRIPT = '''
on run argv
    try
        return POSIX path of (choose folder with prompt (item 1 of argv))
    on error number -128
        return ""
    end try
end run
'''


def choose_folder(storage=False):
    if os.name != 'nt' and sys.platform != 'darwin':
        raise RuntimeError('The native folder picker requires a Windows or macOS server')
    if not _lock.acquire(blocking=False):
        raise RuntimeError('A folder picker is already open on the server')
    try:
        if sys.platform == 'darwin':
            prompt = ('FamilyFoto: select a storage folder (save settings to apply)' if storage
                      else 'FamilyFoto: select an import source folder (selection does not start import)')
            result = subprocess.run(['/usr/bin/osascript', '-e', MAC_SCRIPT, prompt],
                                    capture_output=True, timeout=300)
            if result.returncode:
                raise RuntimeError('Unable to open the folder picker. Use the signed-in Mac desktop, '
                                   'check macOS permissions, or type the path.')
            return result.stdout.decode('utf-8').strip() or None
        script=SCRIPT.replace('FamilyFoto: select an import source folder (selection does not start import)',
                              'FamilyFoto: select a storage folder (save settings to apply)') if storage else SCRIPT
        result = subprocess.run(['powershell.exe','-NoProfile','-STA','-Command',script],
            capture_output=True,timeout=300,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise RuntimeError('Unable to open the folder picker. Use the signed-in Windows server desktop, or type the path.')
        return result.stdout.decode('utf-8-sig').strip() or None
    except subprocess.TimeoutExpired:
        raise RuntimeError('Folder selection timed out after five minutes. Click Browse to try again.')
    finally:
        _lock.release()
