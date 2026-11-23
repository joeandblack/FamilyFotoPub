"""Cooperative local stop signal for uvicorn servers."""
import asyncio


async def watch_stop(server, path):
    while not server.should_exit:
        if path.exists():
            server.should_exit = True
            return
        await asyncio.sleep(0.5)
