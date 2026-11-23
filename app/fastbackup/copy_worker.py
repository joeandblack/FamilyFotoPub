"""One isolated media-copy worker; cancellation retains the incomplete attempt."""
import json
import math
import multiprocessing as mp
import queue
import threading
import time

from app.backup.space import SpaceGuard
from app.backup.diagnostics import ExplainedFailure
from .full_copy import copy_media_tree
from .diagnostics import CopyFailure, describe

MAX_MESSAGE = 8192
WORKER_EXIT_SECONDS = 60


def _heartbeat_event(last, idle_seconds):
    event = dict(last)
    if event.get('state') in {'completed', 'failed'}:
        event['state'] = 'waiting'
    else:
        event['state'] = 'running'
        event['elapsed_seconds'] = round(event.get('elapsed_seconds', 0) + idle_seconds, 3)
    return event


def _confirm_exit(process, deadline, cancelled, progress):
    # A completion message precedes interpreter teardown. Keep its exit bounded,
    # but do not mistake a slow clean exit for a failed copy after five seconds.
    started = time.monotonic()
    exit_deadline = min(deadline, started + WORKER_EXIT_SECONDS)
    heartbeat = started
    while process.is_alive():
        now = time.monotonic()
        if cancelled():
            raise InterruptedError('Copy cancelled while waiting for worker exit; incomplete attempt retained')
        if now >= deadline:
            raise TimeoutError('Media copy deadline exceeded while waiting for worker exit; incomplete attempt retained')
        if now >= exit_deadline:
            raise ExplainedFailure(
                'Media copy reported completion, but the worker did not exit within '
                f'{WORKER_EXIT_SECONDS:g} seconds; overall backup remains incomplete.')
        if progress and now - heartbeat >= 5:
            progress(dict(phase='worker-exit', state='waiting', elapsed_seconds=round(now - started, 1)))
            heartbeat = now
        process.join(timeout=min(0.1, max(0, exit_deadline - time.monotonic())))
    process.join(timeout=0)
    if process.exitcode != 0:
        raise ExplainedFailure(
            'Media copy reported completion, but the worker exited unsuccessfully '
            f'(exit code {process.exitcode}); overall backup remains incomplete.')
    return round(time.monotonic() - started, 3)


def _worker(connection, source, inventory, target, allow_unprotected, guard_factory):
    def send(kind, value):
        raw = json.dumps(dict(kind=kind, value=value), ensure_ascii=True).encode()
        if len(raw) > MAX_MESSAGE:
            raise ValueError('Copy message too large')
        connection.send_bytes(raw)
    try:
        result = copy_media_tree(source, inventory, target, offline_confirmed=True,
                                 allow_unprotected=allow_unprotected, guard_factory=guard_factory,
                                 progress=lambda value: send('progress', value))
        send('complete', result)
    except BaseException as error:
        send('error', describe(error))
    finally:
        connection.close()


def copy_in_worker(source, inventory, target, *, offline_confirmed=False, allow_unprotected=False,
                   guard_factory=SpaceGuard, progress=None, cancelled=lambda: False, timeout=7 * 86400):
    if offline_confirmed is not True:
        raise ValueError('Offline confirmation required')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Finite positive copy timeout required')
    if cancelled():
        raise InterruptedError('Copy cancelled before worker start')
    context = mp.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    messages = queue.Queue(maxsize=64)
    stopped = threading.Event()
    def read():
        try:
            while not stopped.is_set():
                message = json.loads(parent.recv_bytes(MAX_MESSAGE))
                while not stopped.is_set():
                    try:
                        messages.put(message, timeout=0.1)
                        break
                    except queue.Full:
                        pass
        except (EOFError, OSError, ValueError):
            pass
    process = context.Process(target=_worker, args=(
        child, str(source), str(inventory), str(target), allow_unprotected, guard_factory))
    reader = threading.Thread(target=read, daemon=True)
    started = False
    deadline = time.monotonic() + timeout
    heartbeat = time.monotonic()
    last_progress = dict(phase='media-preparation', state='running', elapsed_seconds=0)
    progress_received = heartbeat
    result = None
    try:
        process.start()
        started = True
        child.close()
        reader.start()
        while result is None:
            if cancelled():
                raise InterruptedError('Copy cancelled; incomplete attempt retained')
            if time.monotonic() >= deadline:
                raise TimeoutError('Media copy deadline exceeded; incomplete attempt retained')
            try:
                message = messages.get(timeout=0.1)
            except queue.Empty:
                if not process.is_alive() and not reader.is_alive():
                    raise ExplainedFailure(
                        f'Copy worker exited without completion (exit code {process.exitcode}); '
                        'incomplete attempt retained.')
                message = None
            if message:
                if message['kind'] == 'complete':
                    result = message['value']
                elif message['kind'] == 'error':
                    raise CopyFailure(message['value'])
                elif message['kind'] == 'progress' and progress:
                    last_progress = message['value']
                    progress(last_progress)
                    heartbeat = time.monotonic()
                    progress_received = heartbeat
            if progress and time.monotonic() - heartbeat >= 5:
                progress(_heartbeat_event(last_progress, time.monotonic() - progress_received))
                heartbeat = time.monotonic()
        exit_seconds = _confirm_exit(process, deadline, cancelled, progress)
        if cancelled() or time.monotonic() >= deadline:
            raise InterruptedError('Capture stopped before accepting media completion')
        if progress:
            progress(dict(phase='worker-exited', elapsed_seconds=exit_seconds, exit_code=process.exitcode))
        return result
    finally:
        stopped.set()
        child.close()
        if started:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)
            if process.is_alive():
                raise RuntimeError('Copy worker shutdown unconfirmed; do not release maintenance')
        parent.close()
        if reader.ident is not None:
            reader.join(timeout=2)
            if reader.is_alive():
                raise RuntimeError('Copy reader shutdown unconfirmed')
        if started:
            process.close()
