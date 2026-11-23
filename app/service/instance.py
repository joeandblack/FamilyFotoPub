"""The names one FamilyFoto installation owns on its computer.

A computer can hold more than one installation, for example the library in use and a second one
to try a restore. Each needs its own database service, server task, desktop shortcut and sign-in
cookie: browsers keep cookies by host, not by port, so two installations on 127.0.0.1 would
otherwise sign each other out. An installation made without a name keeps the original names, so
the first installation on a computer, and every one installed before names existed, is unchanged.

The installer (installer/windows/setup-common.ps1, Get-SetupNames) derives the same names.
"""
import re

NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,23}')
DEFAULT = {'database_service': 'FamilyFotoPostgres', 'server_task': 'FamilyFoto Desktop Server',
           'shortcut': 'FamilyFoto Server', 'session_cookie': 'familyfoto_session',
           'backup_cookie': 'familyfoto_backup_run'}


def instance_name(config):
    name = (config or {}).get('instance')
    if name is None:
        return None
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('The installation name must be 1 to 24 lowercase letters, digits or hyphens')
    return name


def names(config):
    name = instance_name(config)
    if name is None:
        return dict(DEFAULT)
    cookie = name.replace('-', '_')
    return {'database_service': f'FamilyFotoPostgres-{name}', 'server_task': f'FamilyFoto Desktop Server ({name})',
            'shortcut': f'FamilyFoto Server ({name})', 'session_cookie': f'familyfoto_session_{cookie}',
            'backup_cookie': f'familyfoto_backup_run_{cookie}'}


def session_cookie(config):
    return names(config)['session_cookie']


def backup_cookie(config):
    """The token that lets the browser that started a backup follow and decide it."""
    return names(config)['backup_cookie']
