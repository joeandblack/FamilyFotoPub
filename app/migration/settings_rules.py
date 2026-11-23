"""Which settings a library moved to a new computer keeps, and which belong to the computer.

A restored `local.json` holds the library's own settings (accounts, preferences) beside ones
that only made sense on the old computer: its address, its FFmpeg, its face engine's paths.
Kept as they were, those broke the November rehearsal: posters failed on a Windows FFmpeg
path, and the server couldn't start on the old computer's address. So every setting has a
rule here, and a setting without one stops the move before anything is written.
"""
import secrets

# The library's own, from the backup.
BACKUP = 'backup'
# This computer's, from the new installation's own settings.
INSTALLATION = 'installation'
# Made by the restore for this computer: the restored database's login, the certificate
# issued for this computer's names, the media's new place, the activation record.
RESTORE = 'restore'
# Made new, so nothing from the old computer carries over (sign-ins).
NEW = 'new'
# Described how the old computer was deployed; left out.
DROPPED = 'dropped'

RULES = {
    'browser_user': BACKUP,
    'browser_password_hash': BACKUP,
    'browser_accounts': BACKUP,
    'browser_password': BACKUP,
    'io_mibps': BACKUP,
    'conversion_performance': BACKUP,
    'avi_conversion': BACKUP,
    'host': INSTALLATION,
    'port': INSTALLATION,
    'postgres_admin_password': INSTALLATION,
    'video_tools': INSTALLATION,
    'backup': INSTALLATION,
    # Which names this installation owns on its computer (app.service.instance).
    'instance': INSTALLATION,
    # The old computer's engine and model paths. People setup on this computer writes its own.
    'face_detection': INSTALLATION,
    # Retired YuNet/SFace model paths from older installations, not library data.
    'faces': DROPPED,
    'people': DROPPED,
    'database': RESTORE,
    'destination': RESTORE,
    'source': RESTORE,
    'tls': RESTORE,
    'recovery': RESTORE,
    'auth_secret': NEW,
    'session_epoch': NEW,
    'preview': DROPPED,
    'live_existing_catalog': DROPPED,
    'live_native_catalog': DROPPED,
    'limited_metadata_edit': DROPPED,
    'read_only_preview': DROPPED,
}


class UnknownSetting(ValueError):
    pass


def unknown(settings):
    return sorted(name for name in settings if name not in RULES)


def merge(restored, installation, *, extra=None):
    """The moved library's settings for this computer.

    `restored` is the activated recovery's settings: the backup's, plus what the restore
    made for this computer. `installation` is the new installation's own. `extra` adds
    computer settings the installation file doesn't hold (such as `backup.pg_dump`).
    Raises UnknownSetting, before anything is written, for a setting with no rule.
    """
    missing = unknown(restored) + [name for name in unknown(installation) if name not in restored]
    if extra:
        missing += [name for name in unknown(extra) if name not in missing]
    if missing:
        raise UnknownSetting('These settings have no rule for a move to another computer: ' + ', '.join(missing)
                             + '. Nothing was changed. Add a rule in app/migration/settings_rules.py.')
    computer = dict(installation)
    for name, value in (extra or {}).items():
        # Added to a group the installation has, never replacing the group.
        computer[name] = ({**computer[name], **value} if isinstance(value, dict) and isinstance(computer.get(name), dict)
                          else value)
    merged = {}
    for name, rule in RULES.items():
        if rule in (BACKUP, RESTORE) and name in restored:
            merged[name] = restored[name]
        elif rule == INSTALLATION and name in computer:
            merged[name] = computer[name]
    merged['auth_secret'] = secrets.token_urlsafe(48)
    merged['session_epoch'] = secrets.token_hex(16)
    return merged


def explain(merged, restored):
    """Names only, never values: where each setting came from, and what was left out."""
    report = {rule: sorted(name for name in merged if RULES[name] == rule)
              for rule in (BACKUP, INSTALLATION, RESTORE, NEW)}
    report['left_out'] = sorted(name for name in restored if name not in merged or RULES[name] == INSTALLATION)
    return report
