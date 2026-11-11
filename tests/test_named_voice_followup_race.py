"""Confirmed speech continuations survive unrelated activity, with or without a name."""
import asyncio
from pathlib import Path
import time

import pytest
from context_helpers import dialogue

from test_group_voice import SCOPE, event, rig, traces


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [True, False])
async def test_confirmed_voice_followup_survives_new_group_message_with_or_without_name(rig, named):
    engine, provider, _ = rig
    provider.answer.update(mode="text", text="晚上的安排可以慢慢想，先休息一下。")
    first = await engine.handle(event("阿梓，晚上好"))
    assert first["mode"] == "text"
    engine.acknowledge(first["reply_id"], "sent", "90001")
    with engine.groups.db() as db:
        db.execute("UPDATE group_activity SET at=?", (time.time()-10,))
        db.execute("UPDATE group_replies SET at=?", (time.time()-10,))
    engine._name_decisions[SCOPE]=time.monotonic()-10

    provider.answer.update(mode="voice", text="晚安，祝你做个好梦。")
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    request = asyncio.create_task(engine.handle(event(
        "阿梓，发语音说声晚安" if named else "能用语音说一句晚安吗", event_id="2")))
    other = None
    try:
        await asyncio.wait_for(provider.entered.wait(), 1)
        other = asyncio.create_task(engine.handle(event(
            "我刚到家", event_id="3", user_id="20002")))

        async def observed():
            while not any(row["message_id"] == "3" for row in engine.groups.snapshot(SCOPE)):
                await asyncio.sleep(0)

        await asyncio.wait_for(observed(), 1)
    finally:
        provider.release.set()
        result = await request
        if other is not None:
            await other

    assert len(provider.speech) == 1
    assert len(provider.calls) == 2
    tts = traces(engine, "tts")
    assert len(tts) == 1 and tts[0]["trigger"] == ('direct' if named else 'followup')
    assert result["mode"] == "voice" and Path(result["media_path"]).is_file()
    assert tts[0]["status"] == "success"


@pytest.mark.asyncio
async def test_explicit_repeat_preserves_actual_voice_turn_and_allows_same_speech(rig):
    import json
    engine,provider,_=rig
    first=await engine.handle(event('阿梓，发语音说声晚安'))
    assert first['mode']=='voice'
    engine.acknowledge(first['reply_id'],'sent','90001')
    # Fake provider intentionally returns the exact previous response.
    result=await engine.handle(event('再来一次',event_id='repeat'))
    assert result['mode']=='voice' and provider.speech==[first['text'],first['text']]
    data=json.loads(provider.calls[-1][1]['content'].split('\n',1)[1])
    rows=dialogue(provider.calls[-1])
    assert rows[0]['text']=='阿梓，发语音说声晚安'
    assert rows[-1]['text']==first['text'] and rows[-1]['mode']=='voice'
