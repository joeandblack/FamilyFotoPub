"""Cooperative Mac shutdown shared by HTTP admission and media workers."""
import asyncio
import json
import sys
from ..config import ROOT, DATA

STOP = ROOT / 'runtime/macos-stop'
# An administrator's backup asks the service to stop only the web server. The database stays up for the capture.
PAUSE_WEB = ROOT / 'runtime/macos-web-pause'
GATE = DATA / '.recovery-admission/maintenance.json'
PAUSES = ('PIPELINE-PAUSE', 'PAUSE', 'VIDEO-PAUSE', 'AVI-PAUSE', 'PHOTO-DUPLICATE-PAUSE')


def stopping():
    return sys.platform == 'darwin' and STOP.exists()


def require_running():
    if stopping():
        raise RuntimeError('FamilyFoto is stopping; no new media operation can start')


def pause_record():
    """Who asked for the web server to stay down, or None. An unreadable record counts as an empty one."""
    try:
        value = json.loads(PAUSE_WEB.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    # Not os.kill(pid, 0) here: on Windows signal 0 is CTRL_C_EVENT and would interrupt the console.
    from ..backup.admin_runs import process_alive as alive
    return alive(pid)


def backup_running():
    """A backup process is working, so the service must not be stopped under it."""
    record = pause_record()
    return record is not None and process_alive(record.get('pid'))


def web_paused():
    """Whether the web server should stay down. A gone backup process only keeps it down while it holds the gate.

    Nothing else may start the web server then: a held gate blocks startup, and it stays for its owner.
    """
    record = pause_record()
    if record is None:
        return False
    if process_alive(record.get('pid')) or GATE.exists():
        return True
    PAUSE_WEB.unlink(missing_ok=True)
    return False


def request_pause(*, stop=True):
    if stop:
        STOP.touch()
    for name in PAUSES:
        (DATA / name).touch()


async def drain_workers():
    """No deadline and no forced kill. Pending decoders must finish/be reviewed."""
    from ..database import connection
    from ..jobs import work_admission
    def cancel_pending():
        with connection() as db:
            request = work_admission.read()
            if request.get('state') == 'pending':
                work_admission.finish(db, request['token'], 'cancelled', 'Application stopped before worker startup')
    await asyncio.to_thread(cancel_pending)
    def busy():
        with connection() as db:
            # All advisory owners other than pooled idle sessions, including
            # upload dispatch and per-content changes, must release their leases.
            return db.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) AND granted AND pid<>pg_backend_pid()) AS busy").fetchone()['busy']
    while await asyncio.to_thread(busy) or (DATA / 'video-decoder-busy.json').exists():
        await asyncio.sleep(.5)
