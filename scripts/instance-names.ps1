# The names one FamilyFoto installation owns on this computer. The same rules as app/service/instance.py.
# An installation made without a name keeps the original names; a second one on the computer has its own.
function Get-FamilyFotoNames([string]$Instance) {
    if (-not $Instance) {
        return @{ Instance = $null; DatabaseService = 'FamilyFotoPostgres'; ServerTask = 'FamilyFoto Desktop Server'; Shortcut = 'FamilyFoto Server' }
    }
    if ($Instance -cnotmatch '^[a-z0-9][a-z0-9-]{0,23}$') {
        throw 'The installation name must be 1 to 24 lowercase letters, digits or hyphens, starting with a letter or digit.'
    }
    @{ Instance = $Instance; DatabaseService = "FamilyFotoPostgres-$Instance"; ServerTask = "FamilyFoto Desktop Server ($Instance)"; Shortcut = "FamilyFoto Server ($Instance)" }
}

function Get-FamilyFotoConfigNames($Config) {
    $instance = if ($Config.PSObject.Properties.Name -contains 'instance') { [string]$Config.instance } else { $null }
    Get-FamilyFotoNames $instance
}
