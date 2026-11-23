"""Move a library to this new computer in one go: restore a backup into this installation.

The installer's **Restore from a backup** runs this once Core is installed, on an installation
that holds no library yet. It runs every step of the manual recovery in order, and keeps
every check, so that one run ends with the library working here:

1. restore the catalog into this installation's own database server, and the media in place
   (moved within the backup's disk) or as a copy;
2. check the restored library against the backup (a write login and this computer's
   certificate, from the backup's own certificate authority, are made for the check);
3. stop at the switch warning until the person confirms the old computer's FamilyFoto is
   stopped for good, then switch the restored library on;
4. put the backup's thumbnails back;
5. move the library's settings and data into this installation's own folders, merging the
   settings by app.migration.settings_rules;
6. set up People on this computer, if chosen;
7. start FamilyFoto with the installation's own service;
8. rebuild any thumbnail or poster still missing.

Each finished stage is recorded in `restore/MOVE-IN.json`, so running it again carries on from
the stage that stopped. Until stage 5 the installation's own settings carry a recovery marker
that the app refuses to start on (`load_config`), so nothing can start an empty library here,
or the restored one before the switch. The backup opens with the administrator password that
was in use when it was made (docs/BACKUP.md, "Backup password"). The caller asks for it, and
asks again when a stopped restore carries on; only the backup's public identity is recorded.
After the restore it is also the password to sign in with, and the switch checks that.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from typing import Callable

from ..backup.diagnostics import ExplainedFailure
from ..backup.encrypted import read_bounded
from ..backup.postgres import Endpoint
from ..backup.private_storage import create_private_directory
from ..backup.recovery import save_private
from . import settings_rules, restore_stages
from .media_folder import check_media_folder

FORMAT = 'familyfoto-move-in-v1'
STAGES = ('restored', 'checked', 'switched', 'thumbnails', 'installed', 'people', 'started', 'repaired')
MEDIA_CHOICES = ('in-place', 'copy')
PEOPLE_CHOICES = ('install', 'skip')
# What the person chose. A run that carries on must make the same choices.
PLAN_FIELDS = ('backup', 'backup_id', 'media', 'media_directory', 'allow_unprotected_media', 'people', 'names',
               'people_port')
# The marker an installation carries until the moved library is switched on and in place.
GATE = {'state': 'restoring', 'activation_required': True}

WARNING = ('Is your old FamilyFoto still in use?\n'
           'This makes the restored library the one you edit. If the old computer is still running '
           "FamilyFoto, changes made there won't appear here, and the two copies will drift apart. "
           'Stop FamilyFoto on the old computer for good before you continue.')


@dataclass
class Installation:
    """What the platform's installer knows about this installation."""
    root: Path
    admin: Endpoint
    cluster: Path
    pg_restore: Path
    pg_dump: Path
    web_port: int
    people_port: int
    # Runs this installation's database server for the duration, and stops it afterwards.
    database: Callable
    # Starts the installed FamilyFoto service.
    start: Callable
    python: Path = Path(sys.executable)
    # Folders in `data` that belong to the installation, not the library, and stay where they are:
    # on Windows the database cluster (`data/postgres`, running during the move) and the setup
    # record (`data/setup`). The Mac keeps its cluster in `runtime`, so it has none.
    keep_data: tuple = ()
    restore_access: Callable | None = None
    restore_access_stage: str = 'restore-access'


def default_names():
    """This computer's own names, for its certificate. LAN names can be added by the person."""
    names = ['127.0.0.1', 'localhost']
    host = socket.gethostname().lower()
    if host and host not in names:
        names.append(host if host.endswith('.local') or '.' in host else host + '.local')
    return names


class WrongBackupPassword(ExplainedFailure):
    """The password doesn't open this backup. A person can type it again."""


WRONG_PASSWORD = ("The password is incorrect, or the backup's password information is damaged. Enter the "
                  'FamilyFoto administrator password from the old computer, as it was when this backup was made.')


def unlock_backup(backup, password):
    """The backup's key, from its administrator password, before anything is recorded.

    `password` is the text as typed. Only a well-formed password file costs a KDF run, and a
    password that opens it must still open the small sealed index, so a wrong password and a
    damaged backup are told apart where they can be."""
    from ..backup import passwords
    from ..backup.encrypted import InvalidArtifact, verify_artifact
    backup = Path(os.path.abspath(backup))
    if not (backup / 'library').is_dir():
        raise ExplainedFailure(f'{backup} is not a finished FamilyFoto backup: it has no library index.')
    try:
        key = passwords.unlock_context(backup, passwords.BackupPassword(password))
    except passwords.WrongPassword:
        raise WrongBackupPassword(WRONG_PASSWORD) from None
    except passwords.UnsuitablePassword as error:
        raise WrongBackupPassword(str(error)) from None
    try:
        verify_artifact(backup / 'library', key)
    except InvalidArtifact:
        raise ExplainedFailure(f'The backup in {backup} opens with this password, but its library index is damaged. '
                               'Use another backup, or make a new one.') from None
    return key


def restored_password_matches(target, password):
    """After the restore the old administrator password is also the sign-in password: the restored
    settings must accept it. Anything else means a damaged or substituted backup."""
    from ..backup.passwords import accepts
    try:
        config = json.loads(read_bounded(Path(target) / 'catalog/files/config/local.json', 1024 ** 2))
    except (OSError, ValueError):
        return False
    encoded = config.get('browser_password_hash')
    return isinstance(encoded, str) and accepts(encoded, password)


def make_plan(backup, password, media, media_directory, *, people, allow_unprotected_media=False, names=None,
              people_port=54341):
    """Check the person's choices before anything is made. Paths are made absolute. `password` is
    checked against the backup and never recorded: the plan keeps the backup's public identity."""
    if media not in MEDIA_CHOICES:
        raise ValueError('Choose how to restore the photos and videos: in-place or copy')
    if people not in PEOPLE_CHOICES:
        raise ValueError('Choose whether to install People')
    if type(people_port) is not int or not 49152 <= people_port <= 65535:
        raise ValueError('The People port must be between 49152 and 65535')
    backup, media_directory = (Path(os.path.abspath(p)) for p in (backup, media_directory))
    if not (backup / 'catalog').is_dir():
        raise ExplainedFailure(f'{backup} is not a FamilyFoto backup folder: it has no catalog.')
    # Before the journal is written: a wrong password or folder is refused now, not once the restore has begun.
    check_media_folder(backup, media, media_directory, allow_unprotected=allow_unprotected_media)
    key = unlock_backup(backup, password)
    return {'backup': str(backup), 'backup_id': key.backup_id, 'media': media, 'media_directory': str(media_directory),
            'allow_unprotected_media': bool(allow_unprotected_media), 'people': people,
            'names': list(names) if names else default_names(), 'people_port': people_port}


class Move:
    def __init__(self, installation, progress=None):
        self.installation = installation
        self.root = Path(installation.root)
        self.work = self.root / 'restore'
        self.journal_path = self.work / 'MOVE-IN.json'
        self.target, self.role, self.tls, self.intent = (self.work / name for name in ('library', 'role', 'tls', 'intent'))
        self.progress = progress or (lambda event: None)
        self.journal = None

    # The record of what is done.

    def begun(self):
        return self.journal_path.is_file()

    def open(self, plan=None):
        """Start a new move, or carry on with the recorded one. A carried-on move keeps its choices."""
        if self.begun():
            journal = json.loads(read_bounded(self.journal_path, 1024 ** 2))
            if journal.get('format') != FORMAT:
                raise ExplainedFailure('The restore record in this installation is not one this version knows.')
            if plan is not None:
                changed = [name for name in PLAN_FIELDS if name in plan and plan[name] != journal['plan'].get(name)]
                if changed:
                    raise ExplainedFailure('This restore was started with other choices (' + ', '.join(changed)
                                           + '). Run it again with the same ones, or without them to keep the recorded ones.')
            self.journal = journal
            self.installation.people_port = journal['plan']['people_port']
            return journal['plan']
        if plan is None:
            raise ExplainedFailure('No restore has been started in this installation.')
        self.require_empty()
        self.installation.people_port = plan['people_port']
        create_private_directory(self.work)
        self.journal = {'format': FORMAT, 'plan': plan, 'stages': {}, 'results': {}}
        self.save()
        # From here the app refuses to start until the restored library is switched on and in place.
        config = self.installation_settings()
        config['recovery'] = dict(GATE)
        save_private(self.root / 'config/local.json', config)
        return plan

    def require_empty(self):
        """The installation, not the computer, must be empty: never adopt or change another library."""
        config = self.installation_settings()
        if config.get('recovery') or any(config.get(name) for name in ('source', 'destination')):
            raise ExplainedFailure('This installation already has a library. Restore into a new installation.')
        thumbnails = self.root / 'data/thumbnails'
        if (self.root / 'data/people').exists() or (thumbnails.is_dir() and any(thumbnails.iterdir())):
            raise ExplainedFailure('This installation already holds library data. Restore into a new installation.')
        if self.work.exists():
            raise ExplainedFailure(f'{self.work} already exists without a restore record. Look at it before trying again.')

    def installation_settings(self):
        return json.loads(read_bounded(self.root / 'config/local.json', 1024 ** 2))

    def save(self):
        save_private(self.journal_path, self.journal)

    def needs_backup(self):
        """Only the stages up to the thumbnails read the backup (and so its password). Once they are
        done, everything left works from the local copy, so a disconnected backup disk, or no
        password, does not stop it."""
        return restore_stages.needs_backup(self.journal['stages'])

    def done(self, stage):
        return stage in self.journal['stages']

    def finish(self, stage, *, progress_stage=None, **results):
        from datetime import datetime, timezone
        self.journal['stages'][stage] = datetime.now(timezone.utc).isoformat()
        self.journal['results'].update(results)
        self.journal.pop('waiting', None)
        self.save()
        self.progress({'stage': progress_stage or stage, 'state': 'done'})

    # The stages.

    def run(self, key, confirm, password=None):
        """Carry the move as far as it can go. `confirm(summary)` asks the switch warning, and
        returns True only when the person confirms. Returns the state reached.

        `key` is unlocked from the recorded backup with its administrator password, asked for
        again on every run. `password`, the same text, lets the switch check that the restored
        settings sign in with it."""
        plan = self.journal['plan']
        backup = Path(plan['backup'])
        inst = self.installation
        if self.needs_backup() and getattr(key, 'backup_id', None) != plan.get('backup_id'):
            raise ExplainedFailure('This password opens another backup than the one this restore started with.')
        if not self.done('people'):
            with inst.database():
                fresh = False
                if not self.done('restored'):
                    self.restore(backup, key, plan)
                    fresh = True
                if not self.done('checked'):
                    self.check(backup, key, plan, fresh)
                if not self.done('switched'):
                    summary = {name: self.journal['results'].get(name) for name in ('media_files', 'media_bytes')}
                    if password is not None and not restored_password_matches(self.target, password):
                        raise ExplainedFailure("The restored library's administrator password is not the one that "
                                               'opened the backup, so the backup may be damaged or replaced. '
                                               'Nothing was switched on.')
                    if not confirm(summary):
                        self.journal['waiting'] = 'switch-warning'
                        self.save()
                        return {'state': 'restored-not-switched', **summary}
                    self.switch(backup, key)
                if not self.done('thumbnails'):
                    self.restore_thumbnails(backup, key)
                if not self.done('installed'):
                    self.install()
                self.restore_access()
                if not self.done('people'):
                    self.set_up_people(plan)
        else:
            # A Windows restore completed by an older installer may still need the ACL repair.
            self.restore_access()
        if not self.done('started'):
            self.progress({'stage': 'started', 'state': 'running'})
            inst.start()
            self.finish('started')
        if not self.done('repaired'):
            self.repair_thumbnails()
        report = self.report()
        save_private(self.work / 'MOVE-IN-REPORT.json', report)
        return report

    def restore_access(self):
        repair = self.installation.restore_access
        stage = self.installation.restore_access_stage
        if repair and not self.done(stage):
            self.progress({'stage': 'restore-access', 'state': 'running'})
            repair()
            self.finish(stage, progress_stage='restore-access')

    def restore(self, backup, key, plan):
        from ..backup.admin_restore import discard_unstarted
        from ..backup.library_restore import restore_library
        inst = self.installation
        self.progress({'stage': 'restored', 'state': 'running'})
        discard_unstarted(self.target / 'catalog')
        discard_unstarted(self.target)
        if (self.target / 'LIBRARY-RECOVERY-REPORT.json').exists():
            # The report is written last, so a marker that outlived it was all that was left to do.
            (self.target / 'LIBRARY-RESTORE-INCOMPLETE').unlink(missing_ok=True)
            report = json.loads(read_bounded(self.target / 'LIBRARY-RECOVERY-REPORT.json', 1024 ** 2))
        else:
            from ..backup.library import verify_library_preflight
            from ..backup.encrypted import verify_artifact
            verify_library_preflight(backup, key)
            database_bytes = verify_artifact(backup / 'catalog/database', key)['size']
            report = restore_library(backup, key, self.target, inst.admin, inst.pg_restore,
                                     expected_cluster_directory=inst.cluster,
                                     database_budget=database_bytes * 4 + 256 * 1024 ** 2,
                                     resume=self.target.exists(), progress=self.progress, validation_follows=True,
                                     in_place=plan['media'] == 'in-place', media_directory=Path(plan['media_directory']),
                                     allow_unprotected_media=plan['allow_unprotected_media'])
        self.finish('restored', media_files=report['media_files'], media_bytes=report['media_bytes'],
                    media_root=report['media_root'], thumbnails_in_backup=report.get('thumbnails'))

    def check(self, backup, key, plan, fresh):
        from ..backup.admin_restore import discard_unstarted
        from .credentials import prepare_recovery_role
        from .tls import prepare_recovery_tls
        from .validation import validate_plain_candidate
        inst = self.installation
        self.progress({'stage': 'checked', 'state': 'running'})
        record = json.loads(read_bounded(self.target / 'catalog/database-target.json', 65536))
        discard_unstarted(self.role)
        prepare_recovery_role(inst.admin, record, self.role, expected_cluster_directory=inst.cluster,
                              resume=self.role.exists())
        if not (self.tls / 'TLS-REPORT.json').exists():
            # Unfinished certificates can't be told from good ones, and these are cheap to issue again.
            shutil.rmtree(self.tls, ignore_errors=True)
            prepare_recovery_tls(self.target / 'catalog/files/config/tls', self.tls, plan['names'])
        result = validate_plain_candidate(backup, key, self.target, inst.admin, self.role, self.tls,
                                          expected_cluster_directory=inst.cluster, progress=self.progress,
                                          fresh_restore=fresh)
        if result['activation_allowed'] is not False or result['services_started'] is not False:
            raise RuntimeError('The restored copy is not isolated')
        self.finish('checked', library_id=result['library_id'], restoration_id=result['restoration_id'])

    def switch(self, backup, key):
        from .activation import SWITCH_WARNING, prepare_activation
        from .activation_commit import commit_activation, sealed_record
        from .activation import FORMAT as INTENT_FORMAT
        inst = self.installation
        self.progress({'stage': 'switched', 'state': 'running'})
        if self.intent.exists() and not (self.intent / 'switch-journal').exists():
            try:
                sealed_record(self.intent, key, INTENT_FORMAT)
            except Exception:
                # Sealed last, so an intent that doesn't open was never finished, and nothing used it.
                shutil.rmtree(self.intent)
        if not self.intent.exists():
            prepare_activation(backup, key, self.target, inst.admin, self.role, self.tls, self.intent,
                               expected_cluster_directory=inst.cluster, accepted=list(SWITCH_WARNING),
                               source_unavailable=True, hostname='127.0.0.1', port=inst.web_port)
        if not (self.intent / 'ACTIVATION-REPORT.json').exists():
            commit_activation(self.intent, key, inst.admin, offline_confirmed=True,
                              resume=(self.intent / 'switch-journal').exists())
        self.finish('switched')

    def restore_thumbnails(self, backup, key):
        from ..backup.thumbnails import restore_thumbnails
        self.progress({'stage': 'thumbnails', 'state': 'running'})
        try:
            counts = restore_thumbnails(backup, key, self.target, progress=self.progress)
        except ExplainedFailure as reason:
            # A backup from before backups kept thumbnails: they are all rebuilt at the end.
            counts = {'none': str(reason)}
        self.finish('thumbnails', thumbnails=counts)

    def install(self):
        """Put the switched-on library into this installation's own folders. Each move is one
        rename on the installation's disk, and the settings are written last, so a stop part way
        carries on from where it was."""
        inst = self.installation
        self.progress({'stage': 'installed', 'state': 'running'})
        files = self.target / 'catalog/files'
        restored = json.loads(read_bounded(files / 'config/local.json', 1024 ** 2))
        kept = self.work / 'installation-local.json'
        if not kept.exists():
            save_private(kept, self.installation_settings())
        installation = json.loads(read_bounded(kept, 1024 ** 2))
        installation.pop('recovery', None)
        merged = settings_rules.merge(restored, installation, extra={'backup': {'pg_dump': str(inst.pg_dump)}})
        merged['tls'] = {'certfile': str(self.root / 'config/tls/server.pem'),
                         'keyfile': str(self.root / 'config/tls/server-key.pem')}
        # The certificate's report is the restore's record, not a setting: it stays with the other
        # records in `restore/`. In config/tls a backup would find a file it has no rule for, and stop.
        report = self.tls / 'TLS-REPORT.json'
        if report.exists():
            os.replace(report, self.work / 'TLS-REPORT.json')
        # The installer's own certificate and data are kept aside, never deleted.
        self.replace(self.root / 'config/tls', self.tls, self.work / 'installation-tls')
        self.move_data(files / 'data')
        self.rebind_people_port()
        save_private(self.root / 'config/local.json', merged)
        self.finish('installed', settings=settings_rules.explain(merged, restored))

    @staticmethod
    def replace(place, new, aside):
        if not new.exists():
            return  # Moved by an earlier run.
        if place.exists() or place.is_symlink():
            if aside.exists():
                raise ExplainedFailure(f'Both {place} and {aside} exist; look at them before trying again.')
            os.rename(place, aside)
        os.rename(new, place)

    def move_data(self, restored):
        """The library's data into this installation's `data`, one entry at a time, so the folders
        the installation owns (Installation.keep_data) stay in place. The installer's other entries
        go aside first, never deleted. Every step is one rename on one disk, and is skipped when an
        earlier run did it."""
        data, aside = self.root / 'data', self.work / 'installation-data'
        keep = set(self.installation.keep_data)
        if restored.exists():
            clash = sorted(entry.name for entry in restored.iterdir() if entry.name in keep)
            if clash:
                raise ExplainedFailure('The backup holds ' + ', '.join(clash) + ' in its data folder, which this '
                                       'installation keeps for itself. Nothing was moved.')
        moved_aside = aside / '.all-moved'
        if not moved_aside.exists():
            # Until the marker is written, everything in `data` but the kept folders is the installer's.
            aside.mkdir(mode=0o700, exist_ok=True)
            for entry in sorted(data.iterdir()) if data.exists() else ():
                if entry.name not in keep:
                    os.rename(entry, aside / entry.name)
            save_private(moved_aside, {'kept_in_place': sorted(keep)})
        if not restored.exists():
            return  # Moved by an earlier run.
        data.mkdir(mode=0o700, exist_ok=True)
        for entry in sorted(restored.iterdir()):
            if (data / entry.name).exists() or (data / entry.name).is_symlink():
                raise ExplainedFailure(f'{data / entry.name} is already there; look at it before trying again.')
            os.rename(entry, data / entry.name)
        restored.rmdir()

    def rebind_people_port(self):
        """The old computer's People port means nothing here: point it at this installation's."""
        path = self.root / 'data/people-preview.json'
        if path.is_file():
            value = json.loads(read_bounded(path, 65536))
            if isinstance(value, dict) and value.get('port') != self.installation.people_port:
                save_private(path, {**value, 'port': self.installation.people_port})

    def set_up_people(self, plan):
        if plan['people'] != 'install':
            if (self.root / 'data/people-preview.json').is_file():
                # The library has People, so it is served view-only, which needs the application's
                # own People libraries. No face model is installed.
                self.progress({'stage': 'people', 'state': 'running'})
                result = self.child('people-view', '-c', 'from face_detect.cli.setup import install_view_dependencies; '
                                    'install_view_dependencies()')
                if result.returncode:
                    raise ExplainedFailure('Installing what viewing People needs stopped; see '
                                           'logs/move-in-people-view.log. Run the restore again to retry it.')
            self.finish('people', people='not installed')
            return
        self.progress({'stage': 'people', 'state': 'running'})
        output = self.work / 'people-setup.json'
        output.unlink(missing_ok=True)
        result = self.child('people-setup', '-m', 'app.migration.people_setup', '--installation', '--research-only',
                            '--port', str(self.installation.people_port), '--report', str(output))
        try:
            check = json.loads(read_bounded(output, 65536)).get('library_check')
        except (OSError, ValueError, AttributeError):
            check = None
        if result.returncode and not isinstance(check, dict):
            raise ExplainedFailure('People setup stopped; see logs/move-in-people-setup.log. '
                                   'Run the restore again to retry it.')
        self.finish('people', people={'library_check': check})

    def repair_thumbnails(self):
        self.progress({'stage': 'repaired', 'state': 'running'})
        result = self.child('thumbnail-repair', '-m', 'scripts.rebuild_missing_thumbnails', '--apply')
        lines = [line for line in result.stdout.splitlines() if line.startswith('{')]
        if result.returncode or not lines:
            raise ExplainedFailure('Rebuilding the missing thumbnails stopped; see logs/move-in-thumbnail-repair.log. '
                                   'FamilyFoto is running. Run the restore again to retry it.')
        self.finish('repaired', repair=json.loads(lines[-1]))

    def child(self, name, *arguments):
        """A step in its own process, with this installation's settings, logged in its logs folder."""
        log = self.root / f'logs/move-in-{name}.log'
        environment = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C'}
        for variable in ('FAMILYFOTO_CONFIG_DIR', 'FAMILYFOTO_DATA_DIR', 'FAMILYFOTO_LOG_DIR', 'FAMILYFOTO_TEMP_DIR'):
            environment.pop(variable, None)
        with log.open('a', encoding='utf-8') as output:
            result = subprocess.run([str(self.installation.python), *arguments], cwd=self.root, env=environment,
                                    stdout=subprocess.PIPE, stderr=output, text=True, stdin=subprocess.DEVNULL)
            output.write(result.stdout)
        return result

    def report(self):
        results = self.journal['results']
        return {'state': 'done', 'url': f'https://127.0.0.1:{self.installation.web_port}',
                'media_files': results.get('media_files'), 'media_bytes': results.get('media_bytes'),
                'media_root': results.get('media_root'), 'thumbnails': results.get('thumbnails'),
                'repair': results.get('repair'), 'people': results.get('people'),
                'certificate_names': self.journal['plan']['names'],
                'certificate_authority': str(self.root / 'config/tls/ca.pem'),
                'settings': results.get('settings')}

