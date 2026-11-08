"""Named starts and member-scoped follow-ups; synthetic providers only."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from context_helpers import context as wire_context

from azusa_bot.bot_control import BotPowerController
from test_group_followup import rig, event, configure, calls, SCOPE, IDENTITY
from test_group_voice import rig as voice_rig, event as voice_event


async def named_start(engine,provider,clock,*,ack=True):
    engine.address_names_loader=lambda:["小z"]
    provider.passive_reply={"participate":True,"confidence":.99,"mode":"text",
                            "text":"第一步先列出输入和输出。"}
    reply=await engine.handle(event("小z，这个计算过程怎么理解",event_id="named-start"))
    assert reply["mode"]=="text"
    if ack:
        engine.acknowledge(reply["reply_id"],"sent","90001")
        assert IDENTITY in engine._group_threads
    clock.advance(6)
    return reply


@pytest.mark.asyncio
async def test_other_member_cannot_merge_away_confirmed_followup(rig):
    engine,settings,provider,clock=rig
    await named_start(engine,provider,clock)
    configure(engine,settings,group_chat_debounce_seconds=.04)
    first=asyncio.create_task(engine.handle(event("那第二步呢",event_id="followup")))
    await asyncio.sleep(.01)
    other=asyncio.create_task(engine.handle(event("大家今晚几点打游戏",user_id="20002",event_id="other")))
    reply,unrelated=await asyncio.gather(first,other)
    assert reply["mode"]=="text" and unrelated["mode"]=="none"
    assert len(calls(provider,"followup"))==1


@pytest.mark.asyncio
async def test_other_member_cannot_discard_paid_followup(rig):
    engine,_,provider,clock=rig
    await named_start(engine,provider,clock)
    provider.entered,provider.release=asyncio.Event(),asyncio.Event()
    first=asyncio.create_task(engine.handle(event("那第二步呢",event_id="followup")))
    await asyncio.wait_for(provider.entered.wait(),2)
    other=asyncio.create_task(engine.handle(event("大家今晚几点打游戏",user_id="20002",event_id="other")))
    await asyncio.sleep(0)
    provider.release.set()
    reply,unrelated=await asyncio.gather(first,other)
    assert reply["mode"]=="text" and unrelated["mode"]=="none"
    assert len(calls(provider,"followup"))==1


@pytest.mark.asyncio
async def test_own_new_message_still_cancels_inflight_stale_reply(rig):
    engine,_,provider,clock=rig
    await named_start(engine,provider,clock)
    provider.entered,provider.release=asyncio.Event(),asyncio.Event()
    first=asyncio.create_task(engine.handle(event("那用哪个数",event_id="stale")))
    await asyncio.wait_for(provider.entered.wait(),2)
    second=asyncio.create_task(engine.handle(event("我补充一下，用三来举例",event_id="fresh")))
    await asyncio.sleep(0)
    clock.advance(6)  # The already-started first request still counts toward pacing.
    provider.release.set()
    old,fresh=await asyncio.gather(first,second)
    assert old["mode"]=="none" and fresh["mode"]=="text"
    assert len(calls(provider,"followup"))==2
    assert fresh["reply_id"] in engine._group_pending and old["reply_id"] not in engine._group_pending


@pytest.mark.asyncio
async def test_same_member_burst_merges_to_latest_with_both_parts_in_context(rig):
    engine,settings,provider,clock=rig
    await named_start(engine,provider,clock)
    configure(engine,settings,group_chat_debounce_seconds=.04)
    first=asyncio.create_task(engine.handle(event("那16G够吗",event_id="first-part")))
    await asyncio.sleep(.01)
    second=asyncio.create_task(engine.handle(event("主要拿来剪视频",event_id="second-part")))
    old,fresh=await asyncio.gather(first,second)
    assert old["mode"]=="none" and fresh["mode"]=="text"
    decisions=calls(provider,"followup")
    assert len(decisions)==1
    context=wire_context(decisions[0]["messages"])
    assert {"first-part","second-part"}<={row["message_id"] for row in context["recent_conversation"]+[context["current_message"]]}
    last=json.loads(decisions[0]["messages"][-1]["content"].split("\n",1)[1])
    assert last["current_message"]["text"]=="主要拿来剪视频"


def controlled_wait(monkeypatch,clock):
    entered,release=asyncio.Event(),asyncio.Event()
    delays=[]
    async def sleep(seconds):
        delays.append(seconds)
        entered.set()
        await release.wait()
        clock.advance(seconds)
    monkeypatch.setattr("azusa_bot.core.asyncio",SimpleNamespace(sleep=sleep,
        Lock=asyncio.Lock,Semaphore=asyncio.Semaphore,CancelledError=asyncio.CancelledError))
    return entered,release,delays


async def one_followup(engine,provider,clock):
    await named_start(engine,provider,clock)
    reply=await engine.handle(event("那第二步呢",event_id="first-followup"))
    assert reply["mode"]=="text"
    engine.acknowledge(reply["reply_id"],"sent","90002")
    clock.advance(1)
    provider.followup_reply=dict(provider.followup_reply,text="先把三代入这个位置，再逐项检查结果。")


@pytest.mark.asyncio
async def test_short_paid_interval_waits_and_coalesces_before_another_model_call(rig,monkeypatch):
    engine,_,provider,clock=rig
    await one_followup(engine,provider,clock)
    entered,release,delays=controlled_wait(monkeypatch,clock)
    early=asyncio.create_task(engine.handle(event("具体怎么代入",event_id="early")))
    await asyncio.wait_for(entered.wait(),2)
    assert len(calls(provider,"followup"))==1  # The wait has not spent another request.
    latest=asyncio.create_task(engine.handle(event("比如用数字三",event_id="latest")))
    await asyncio.sleep(0)
    release.set()
    old,fresh=await asyncio.gather(early,latest)
    assert old["mode"]=="none" and fresh["mode"]=="text"
    assert delays==[4.0] and len(calls(provider,"followup"))==2
    context=wire_context(calls(provider,"followup")[-1]["messages"])
    assert {"early","latest"}<={row["message_id"] for row in context["recent_conversation"]+[context["current_message"]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("command",["/旁听 关闭","/记忆 关闭","/忘记我"])
async def test_privacy_withdrawal_during_short_wait_cancels_before_cloud(rig,monkeypatch,command):
    engine,_,provider,clock=rig
    await one_followup(engine,provider,clock)
    entered,release,_=controlled_wait(monkeypatch,clock)
    waiting=asyncio.create_task(engine.handle(event("继续解释",event_id="waiting")))
    await asyncio.wait_for(entered.wait(),2)
    await engine.handle(event(command,event_id="privacy"))
    release.set()
    assert (await waiting)["mode"]=="none"
    assert len(calls(provider,"followup"))==1


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["whitelist","profile","power"])
async def test_policy_change_during_short_wait_cancels_before_cloud(rig,monkeypatch,change):
    engine,settings,provider,clock=rig
    await one_followup(engine,provider,clock)
    entered,release,_=controlled_wait(monkeypatch,clock)
    waiting=asyncio.create_task(engine.handle(event("继续解释",event_id="waiting")))
    await asyncio.wait_for(entered.wait(),2)
    if change=="whitelist": configure(engine,settings,onebot_allowed_groups=[])
    elif change=="profile": engine.invalidate_groups()
    else: BotPowerController(engine.root,engine.invalidate_groups)._write("stopped")
    release.set()
    assert (await waiting)["mode"]=="none"
    assert len(calls(provider,"followup"))==1


@pytest.mark.asyncio
async def test_own_new_message_while_waiting_capacity_does_not_spend_old_request(rig):
    engine,_,provider,clock=rig
    await named_start(engine,provider,clock)
    entered,release=asyncio.Event(),asyncio.Event()
    class Capacity:
        async def __aenter__(self):
            entered.set()
            await release.wait()
        async def __aexit__(self,*_):
            pass
    engine._capacity=Capacity()
    old=asyncio.create_task(engine.handle(event("具体怎么代入",event_id="capacity-old")))
    await asyncio.wait_for(entered.wait(),2)
    assert not calls(provider,"followup")
    new=asyncio.create_task(engine.handle(event("我补充一下，用数字三",event_id="capacity-new")))
    await asyncio.sleep(0)
    release.set()
    stale,fresh=await asyncio.gather(old,new)
    assert stale["mode"]=="none" and fresh["mode"]=="text"
    assert len(calls(provider,"followup"))==1


@pytest.mark.asyncio
@pytest.mark.parametrize("ack",[True,False])
async def test_ack_arriving_after_debounce_before_group_lock_is_rechecked(rig,ack):
    engine,settings,provider,clock=rig
    first=await named_start(engine,provider,clock,ack=False)
    configure(engine,settings,group_chat_debounce_seconds=.01)
    lock=engine.groups.lock(SCOPE)
    async with lock:
        followup=asyncio.create_task(engine.handle(event("那下一步呢",event_id="after-prepared")))
        await asyncio.sleep(.015)
        other=asyncio.create_task(engine.handle(event("大家今晚见",user_id="20002",event_id="other")))
        await asyncio.sleep(.015)
        if ack:engine.acknowledge(first["reply_id"],"sent","90001")
    reply,_=await asyncio.gather(followup,other)
    assert (reply["mode"]=="text") is ack
    assert len(calls(provider,"followup"))==int(ack)


@pytest.mark.asyncio
async def test_other_member_during_followup_tts_does_not_discard_voice(voice_rig):
    engine,provider,_=voice_rig
    provider.answer.update(mode="text",text="晚安之前记得早点休息。")
    first=await engine.handle(voice_event("阿梓，晚上好"))
    engine.acknowledge(first["reply_id"],"sent","90001")
    with engine.groups.db() as db:
        db.execute("UPDATE group_activity SET at=0")
        db.execute("UPDATE group_replies SET at=0")
    provider.answer.update(mode="voice",text="祝你今晚有个好梦，明天见。")
    provider.entered,provider.release=asyncio.Event(),asyncio.Event()
    followup=asyncio.create_task(engine.handle(voice_event("能用语音再说一句吗",event_id="2")))
    await asyncio.wait_for(provider.entered.wait(),2)
    other=asyncio.create_task(engine.handle(voice_event("大家今晚见",event_id="3",user_id="20002")))
    await asyncio.sleep(0)
    provider.release.set()
    reply,_=await asyncio.gather(followup,other)
    assert reply["mode"]=="voice" and len(provider.speech)==1
