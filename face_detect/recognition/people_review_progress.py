"""Stream progress without treating uncommitted face updates as completed work."""
import asyncio
import json
import logging
from threading import Event
from fastapi import HTTPException

_running = set()


async def stream_review(run, heartbeat=5):
    loop = asyncio.get_running_loop()
    updates = asyncio.Queue()
    detached = Event()

    def report(phase, done, total):
        if not detached.is_set():
            loop.call_soon_threadsafe(updates.put_nowait,
                                     {'state': 'running', 'phase': phase, 'done': done, 'total': total})

    async def work():
        try:
            result = await asyncio.to_thread(run, report)
            # invoke returns only after its database connection commits successfully.
            await updates.put({'state': 'completed', 'result': result})
        except HTTPException as error:
            await updates.put({'state': 'failed', 'message': error.detail, 'status': error.status_code})
        except Exception:
            logging.getLogger(__name__).exception('People bulk review failed')
            await updates.put({'state': 'failed', 'message':
                'Could not confirm completion. Refresh People to check the results before trying again.'})

    task = asyncio.create_task(work())
    _running.add(task)
    task.add_done_callback(_running.discard)
    current = {'state': 'running', 'phase': 'starting', 'done': 0, 'total': None}
    try:
        yield json.dumps(current) + '\n'
        while True:
            try:
                current = await asyncio.wait_for(updates.get(), heartbeat)
            except asyncio.TimeoutError:
                pass  # Keep the connection alive while a lock or database call waits.
            yield json.dumps(current) + '\n'
            if current['state'] in ('completed', 'failed'):
                break
    finally:
        # A disconnected browser must not cancel an in-flight database transaction.
        detached.set()
