import asyncio
import json
import time
from threading import Event
from fastapi import HTTPException
from face_detect.recognition.people_review_progress import stream_review
from face_detect.recognition.people_review_progress import _running


def test_heartbeats_and_processing_do_not_claim_completion_before_commit():
    saved = Event()
    def run(report):
        report('waiting',0,None)
        time.sleep(.04)
        report('processing',1,2)
        report('processing',2,2)
        report('saving',None,None)
        time.sleep(.04)
        saved.set()
        return {'matched':2}
    async def consume():
        messages=[]
        async for line in stream_review(run,heartbeat=.005):
            message=json.loads(line);messages.append(message)
            if message['state']=='completed':assert saved.is_set()
        return messages
    messages=asyncio.run(consume())
    assert len(messages)>6
    assert any(m.get('phase')=='waiting' for m in messages)
    assert any(m.get('done')==2 for m in messages)
    assert messages[-1]=={'state':'completed','result':{'matched':2}}


def test_known_failure_streams_error_not_success():
    def run(report):
        report('processing',1,3)
        raise HTTPException(409,'Selected faces changed')
    async def consume():return [json.loads(line) async for line in stream_review(run)]
    messages=asyncio.run(consume())
    assert messages[-1]=={'state':'failed','message':'Selected faces changed','status':409}
    assert not any(m['state']=='completed' for m in messages)


def test_disconnect_does_not_cancel_inflight_work():
    finished=Event()
    def run(report):
        time.sleep(.03);report('saving',None,None);finished.set();return {}
    async def disconnect():
        stream=stream_review(run)
        await anext(stream)
        await stream.aclose()
        await asyncio.gather(*list(_running))
    asyncio.run(disconnect())
    assert finished.is_set()
