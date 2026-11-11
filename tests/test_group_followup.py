"""ACK-based same-member follow-up conversations, using only controlled providers."""

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from support_content import seed_content

from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import ProviderError, mock_group_plan
from azusa_bot.settings import Settings, write_settings
from azusa_bot.trajectory_admin import attach_delivery
from context_helpers import context as wire_context


SCOPE = "onebot:10001:group:30001"
IDENTITY = (SCOPE, "20001")


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Provider:
    def __init__(self):
        self.calls = []
        self.plan_calls = []
        self.direct_reply = {"text": "先把输入和输出列出来，我们一步一步看。", "mode": "text"}
        self.followup_reply = {"participate": True, "continuation": True, "confidence": .95,
                               "text": "接下来选一个具体数字，顺着计算走一遍。"}
        self.passive_reply = {"participate": False, "confidence": 0., "text": ""}
        self.error_kind = None
        self.block_kind = "followup"
        self.entered = None
        self.release = None

    async def complete(self, messages, *, summary=False):
        data=wire_context(messages)
        if data.get('task')=='group_plan':
            recent=data.get('recent_conversation',[])
            own=[row for row in recent if row.get('role')=='assistant']
            quoted=data['current_message'].get('quoted_message',{})
            direct=quoted.get('user_id')==data['self_id']
            kind='direct' if direct else 'followup' if own else 'passive'
            self.plan_calls.append({'kind':kind,'messages':messages})
            answer=({'participate':True,'confidence':.95}|self.direct_reply) if direct else self.followup_reply if own else self.passive_reply
            result=mock_group_plan(data)
            # These fixtures explicitly supply the semantic assessment. They do
            # not ask the deterministic mock to understand the scripted prose.
            plan=result['participation_plan']
            if own and not direct:
                anchor=own[-1]
                evidence=[data['current_message']['message_id'],anchor['message_id']]
                question=anchor.get('response_to_message_id')
                if question and any(row['message_id']==question for row in recent):evidence.append(question)
                plan.update(target='self',addressee_ids=[data['self_id']],action='reply',
                    response_to_message_id=anchor['message_id'],thread_anchor=question or anchor['message_id'],
                    evidence_message_ids=list(dict.fromkeys(evidence)))
            if (answer.get('participate') is not True or answer.get('continuation',True) is not True
                    or '小王' in data['current_message']['text']):
                plan.update(action='observe',reason='synthetic_other_conversation')
            result.pop('confidence',None);result.pop('votes',None)
            for key in ('confidence','votes'):
                if key in answer:result[key]=answer[key]
            return result
        if summary:
            kind = "summary"
        else:
            # Shared editable policies can mention continuation/participation
            # in every request. Classify the actual structured input envelope.
            kind=('direct' if 'trusted_identity' in data else
                  'followup' if data.get('continuation_candidate') else
                  'passive')
        self.calls.append({"kind": kind, "messages": messages})
        if self.entered and kind == self.block_kind:
            self.entered.set()
        if self.release and kind == self.block_kind:
            await self.release.wait()
        if kind == self.error_kind:
            raise ProviderError("deepseek_network_or_format")
        answer={"summary": {"memories": []}, "followup": self.followup_reply,
                "passive": self.passive_reply, "direct": self.direct_reply}[kind]
        return {key:value for key,value in answer.items() if key not in {'participate','continuation','confidence','votes'}}

    async def synthesize(self, text):
        raise AssertionError("These tests do not use speech services")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    seed_content(tmp_path)
    clock = Clock()
    monkeypatch.setattr("azusa_bot.core.time", SimpleNamespace(time=clock, monotonic=clock,
                         strftime=time.strftime, gmtime=time.gmtime))
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
                        onebot_allowed_groups=["30001", "30002"], onebot_allowed_dm_users=["20001"],
                        group_chat_enabled=True, group_chat_debounce_seconds=0,
                        group_chat_min_messages=20, group_chat_cooldown_seconds=90,
                        group_chat_decision_interval_seconds=30, min_interval_seconds=2,
                        group_followup_enabled=True, group_followup_seconds=180,
                        group_followup_max_turns=2, group_followup_min_interval_seconds=5)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    engine.groups.clock = clock
    provider = Provider()
    engine.provider_override = provider
    yield engine, settings, provider, clock
    engine.groups.close()
    engine.memory.close()


def event(text="那下一步怎么处理", **updates):
    body = dict(transport="onebot", app_id="10001", kind="group", scope_id="30001",
                user_id="20001", event_id="message", text=text, unprompted=True, display_name="同名昵称")
    body.update(updates)
    return Incoming(**body)


def configure(engine, settings, **updates):
    for key, value in updates.items():
        setattr(settings, key, value)
    write_settings(engine.root, settings)


def calls(provider, kind):
    return [call for call in provider.calls if call["kind"] == kind]


def plans(provider, kind):
    return [call for call in provider.plan_calls if call['kind']==kind]


async def start(engine, *, event_id="start", receipt="90001"):
    reply = await engine.handle(event("请帮我解释一下这道计算", mentioned=True, event_id=event_id))
    assert reply["mode"] == "text"
    engine.acknowledge(reply["reply_id"], "sent", receipt)
    assert IDENTITY in engine._group_threads
    return reply


@pytest.mark.asyncio
async def test_sliding_followup_window_does_not_keep_older_raw_turns(rig):
    engine,settings,provider,clock=rig
    configure(engine,settings,group_chat_context_minutes=1)
    await start(engine,event_id="old-turn")
    clock.advance(50)
    second=await engine.handle(event("那这个输入呢",event_id="recent-turn"))
    assert second["mode"]=="text"
    engine.acknowledge(second["reply_id"],"sent","90002")
    clock.advance(20)
    thread=engine._active_thread(SCOPE,"20001",settings)
    assert thread is not None
    assert [r["message_id"] for r in thread["messages"]]==["recent-turn","90002"]
    assert engine._thread_context(thread)[0]["age_seconds"]==20
    provider.followup_reply=dict(provider.followup_reply,text="这一步的结果再传给下一层，就能继续算下去了。")
    third=await engine.handle(event("接着说吧",event_id="latest-turn"))
    assert third["mode"]=="text"
    data=wire_context(provider.calls[-1]['messages'])
    assert 'old-turn' not in [row['message_id'] for row in data['recent_conversation']]
    assert '请帮我解释一下这道计算' not in str(provider.calls[-1]['messages'])
    # The public plan names retained evidence; internal thread identity may
    # outlive an old raw message without reintroducing that message's content.
    assert data['participation_plan']['thread_anchor']=='recent-turn'
    assert 'old-turn' not in data['participation_plan']['evidence_message_ids']


@pytest.mark.asyncio
async def test_delayed_ack_does_not_refresh_incoming_message_age(rig):
    engine,settings,_,clock=rig
    configure(engine,settings,group_chat_context_minutes=1)
    first=await engine.handle(event("原来的图片话题",mentioned=True,event_id="old-input"))
    clock.advance(65)
    engine.acknowledge(first["reply_id"],"sent","90001")
    thread=engine._active_thread(SCOPE,"20001",settings)
    assert thread is not None
    assert all(m["message_id"]!="old-input" for m in thread["messages"])


@pytest.mark.parametrize("confidence,state,reason", [
    (.79,"valid","followup_low_confidence"),
    (None,"missing","conversation_plan_invalid"),
    ("0.95","invalid","conversation_plan_invalid"),
    (True,"invalid","conversation_plan_invalid"),
    (float('nan'),"invalid","conversation_plan_invalid"),
    (1.1,"invalid","conversation_plan_invalid"),
])
async def test_planned_followup_exposes_why_generation_was_not_started(rig,confidence,state,reason):
    engine,_,provider,clock=rig
    await start(engine)
    clock.advance(6)
    provider.followup_reply={"participate":True,"continuation":True,"mode":"text","text":"合成的候选回复"}
    if confidence is not None:
        provider.followup_reply["confidence"]=confidence
    result=await engine.handle(event(event_id="confidence-case"))
    assert result["mode"]=="none" and result["reason"]==reason
    assert not calls(provider,'followup') and len(plans(provider,'followup'))==1
    rows=engine.trajectories.list(kind="group_plan")["items"]
    attach_delivery(engine,rows)
    assert rows[0]["status"]==("success" if state=="valid" else "invalid_output")
    assert rows[0]['response_text'] is None
    assert rows[0]['confidence_state']==state
    assert rows[0].get('confidence')==(.79 if state=='valid' else None)
    if state!='valid':assert rows[0]['error_code']=='conversation_plan_invalid'
    else:assert rows[0]['participation_plan']['action']=='observe'
    assert rows[0]['delivery']['state']=='not_applicable'


async def test_threshold_followup_tracks_prepared_then_confirmed_delivery(rig):
    engine,_,provider,clock=rig
    await start(engine)
    clock.advance(6)
    provider.followup_reply["confidence"]=.8
    result=await engine.handle(event(event_id="threshold-case"))
    assert result["mode"]=="text"
    rows=engine.trajectories.list(kind="followup")["items"]
    assert attach_delivery(engine,rows)[0]["delivery"]["state"]=="unconfirmed"
    engine.acknowledge(result["reply_id"],"sent","123456")
    rows=engine.trajectories.list(kind="followup")["items"]
    assert attach_delivery(engine,rows)[0]["delivery"]["state"]=="sent"


@pytest.mark.asyncio
async def test_window_requires_sent_receipt_not_prepared_uncertain_missing_or_stale_ack(rig):
    engine, _, provider, clock = rig
    prepared = await engine.handle(event("先解释一下", mentioned=True, event_id="prepared"))
    assert IDENTITY not in engine._group_threads
    clock.advance(6)
    assert (await engine.handle(event(event_id="before-ack")))["mode"] == "none"
    assert calls(provider, "followup") == []
    engine.acknowledge(prepared["reply_id"], "uncertain", "91001")
    engine.acknowledge(prepared["reply_id"], "sent", "91001")
    assert IDENTITY not in engine._group_threads
    missing = await engine.handle(event("重新解释", mentioned=True, event_id="missing-receipt"))
    engine.acknowledge(missing["reply_id"], "sent")
    assert IDENTITY not in engine._group_threads
    clock.advance(6)
    stale = await engine.handle(event("再解释一次", mentioned=True, event_id="stale-receipt"))
    clock.advance(241)
    engine.acknowledge(stale["reply_id"], "sent", "91003")
    assert IDENTITY not in engine._group_threads
    await start(engine, event_id="valid", receipt="91004")


@pytest.mark.asyncio
async def test_same_user_can_continue_without_mention_inside_proactive_cooldown(rig):
    engine, settings, provider, clock = rig
    await start(engine)
    assert engine.groups.budget_status(SCOPE, **engine._group_budget(settings))["reason"] == "cooldown"
    clock.advance(6)  # Still inside both the ordinary 90s speaking and 30s decision cooldowns.
    continuation = await engine.handle(event("那第二步用哪个数", event_id="continuation"))
    assert continuation["mode"] == "text"
    assert continuation["text"] == provider.followup_reply["text"]
    assert len(calls(provider, "followup")) == 1 and calls(provider, "passive") == []
    assert engine.groups.budget_status(SCOPE, **engine._group_budget(settings))["day_used"] == 0


@pytest.mark.asyncio
async def test_window_is_scoped_by_group_member_and_logged_account(rig):
    engine, _, provider, clock = rig
    await start(engine)
    clock.advance(6)
    for incoming in (event(user_id="20002", event_id="other-user"),
                     event(scope_id="30002", event_id="other-group"),
                     event(app_id="10002", event_id="other-account")):
        assert (await engine.handle(incoming))["mode"] == "none"
    assert calls(provider, "followup") == []
    # A private message is an independent ordinary conversation, not this group window.
    private = await engine.handle(event("私聊另一个问题", kind="dm", scope_id="20001",
                                        unprompted=False, event_id="private"))
    assert private["mode"] == "text" and len(calls(provider, "direct")) == 2
    assert set(engine._group_threads) == {IDENTITY}


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["other_mentioned", "unknown_quote"])
async def test_addressing_someone_else_does_not_borrow_active_window(rig, signal):
    engine, _, provider, clock = rig
    await start(engine)
    clock.advance(6)
    extra = {"other_mentioned": True} if signal == "other_mentioned" else {"reply_to_id": "88888"}
    unrelated = await engine.handle(event("小王，这个是问你的", event_id="not-for-bot", **extra))
    assert unrelated["mode"] == "none"
    assert calls(provider, "followup") == []


@pytest.mark.asyncio
async def test_semantic_noncontinuation_stays_silent_and_ends_window(rig):
    engine, _, provider, clock = rig
    await start(engine)
    clock.advance(6)
    provider.followup_reply = {"participate": True, "continuation": False, "confidence": .99,
                               "text": "这段绝不能被发出去"}
    unrelated = await engine.handle(event("小王刚才的比赛继续聊吧", event_id="changed-topic"))
    assert unrelated["mode"] == "none" and unrelated["text"] == ""
    assert IDENTITY not in engine._group_threads
    clock.advance(6)
    assert (await engine.handle(event("继续和小王聊", event_id="after-end")))["mode"] == "none"
    assert calls(provider,'followup')==[] and len(plans(provider,'followup'))==1


@pytest.mark.asyncio
async def test_ttl_expires_without_ack_and_valid_followup_ack_refreshes_window(rig):
    engine, _, provider, clock = rig
    await start(engine)
    clock.advance(175)
    prepared = await engine.handle(event("接着解释最后一步", event_id="near-expiry"))
    assert prepared["mode"] == "text"
    clock.advance(10)
    expired = await engine.handle(event("还在原窗口里吗", event_id="expired"))
    assert expired["mode"] == "none" and len(calls(provider, "followup")) == 1
    # A fresh explicit mention starts another conversation, even after expiry.
    await start(engine, event_id="restart", receipt="92001")
    clock.advance(175)
    continued = await engine.handle(event("现在继续讲", event_id="refresh-by-ack"))
    assert continued["mode"] == "text"
    engine.acknowledge(continued["reply_id"], "sent", "92002")
    clock.advance(10)
    provider.followup_reply = {"participate": True, "continuation": True, "confidence": .95,
                               "text": "现在把这个结果代回原式，确认前后保持一致。"}
    refreshed = await engine.handle(event("新窗口里接着说", event_id="after-refresh"))
    assert refreshed["mode"] == "text"
    assert len(calls(provider, "followup")) == 3


@pytest.mark.asyncio
async def test_successful_followup_turn_limit_and_explicit_mention_reopens(rig):
    engine, _, provider, clock = rig
    await start(engine)
    for n, answer in enumerate(("第一步先确认输入包含哪些数据。", "接下来用一个具体数字演示整个过程。")):
        clock.advance(6)
        provider.followup_reply = {"participate": True, "continuation": True, "confidence": .95, "text": answer}
        reply = await engine.handle(event(f"继续解释第{n}处", event_id=f"turn-{n}"))
        assert reply["mode"] == "text"
        engine.acknowledge(reply["reply_id"], "sent", str(93000+n))
    clock.advance(6)
    limited = await engine.handle(event("达到次数之后的一句", event_id="over-limit"))
    assert limited["mode"] == "none" and len(calls(provider, "followup")) == 2
    await start(engine, event_id="explicit-reopen", receipt="93099")
    clock.advance(6)
    assert (await engine.handle(event("重新开始后续聊", event_id="reopened")))["mode"] == "text"
    assert len(calls(provider, "followup")) == 3


@pytest.mark.asyncio
async def test_short_member_cooldown_and_total_cloud_budget_still_apply(rig, monkeypatch):
    engine, settings, provider, clock = rig
    await start(engine)
    delays = []

    async def paced_sleep(seconds):
        delays.append(seconds)
        clock.advance(seconds)
        await asyncio.sleep(0)

    # Patch only core's namespace, leaving the real event loop and debounce
    # helper untouched. Early messages wait rather than forcing users to repeat.
    monkeypatch.setattr("azusa_bot.core.asyncio", SimpleNamespace(sleep=paced_sleep,
                         Lock=asyncio.Lock, Semaphore=asyncio.Semaphore, CancelledError=asyncio.CancelledError))
    clock.advance(1)
    early = await engine.handle(event(event_id="early-but-paced"))
    assert early["mode"] == "text" and delays == [4.0]
    assert len(calls(provider, "followup")) == 1
    pending = await engine.handle(event(event_id="awaiting-send-receipt"))
    assert pending["mode"] == "none" and pending["reason"] == "followup_pending"
    engine.acknowledge(early["reply_id"], "sent", "96001")
    provider.followup_reply = dict(provider.followup_reply,text="这个例子先代入三，再核对最后的结果。")
    paced = await engine.handle(event(event_id="too-close-to-decision"))
    assert paced["mode"] == "text" and delays == [4.0,1.0,4.0]
    assert len(calls(provider, "followup")) == 2
    engine.acknowledge(paced["reply_id"],"sent","96002")
    # Switch to cloud accounting and explicitly exhaust the single daily slot.
    configure(engine, settings, provider_mode="cloud", daily_cloud_requests=1,group_followup_max_turns=3)
    assert engine.ledger.consume(1)
    clock.advance(6)
    limited = await engine.handle(event(event_id="cloud-exhausted"))
    assert limited["mode"] == "none" and limited["text"] == "" and limited["reason"] == "cloud_budget"
    assert len(calls(provider, "followup")) == 2


@pytest.mark.asyncio
async def test_duplicate_concurrent_followup_has_one_decision_and_no_placeholder(rig):
    engine, _, provider, clock = rig
    await start(engine)
    clock.advance(6)
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(engine.handle(event(event_id="same-event")))
    await asyncio.wait_for(provider.entered.wait(), 2)
    replay = await engine.handle(event(event_id="same-event"))
    assert replay["mode"] == "none" and replay["reason"] == "duplicate_event"
    provider.release.set()
    assert (await first)["mode"] == "text"
    assert len(calls(provider, "followup")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/旁听 关闭", "/记忆 关闭", "/忘记我"])
async def test_privacy_command_immediately_revokes_inflight_followup_and_ack_cannot_reopen(rig, command):
    engine, _, provider, clock = rig
    await start(engine)
    clock.advance(6)
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    waiting = asyncio.create_task(engine.handle(event(event_id="pending-followup")))
    await asyncio.wait_for(provider.entered.wait(), 2)
    try:
        privacy = await asyncio.wait_for(engine.handle(event(command, event_id="privacy-command")), .5)
        assert privacy["mode"] == "text" and not waiting.done()
        assert IDENTITY not in engine._group_threads
        engine.acknowledge(privacy["reply_id"], "sent", "94001")
        assert IDENTITY not in engine._group_threads
    finally:
        provider.release.set()
    result = await waiting
    assert result["mode"] == "none" and result["text"] == ""
    engine.acknowledge(result["reply_id"], "sent", "94002")
    assert IDENTITY not in engine._group_threads


@pytest.mark.asyncio
async def test_provider_failure_is_silent_and_command_or_sdk_error_does_not_open_window(rig):
    engine, _, provider, clock = rig
    command = await engine.handle(event("/身份", event_id="identity-command"))
    engine.acknowledge(command["reply_id"], "sent", "95001")
    assert IDENTITY not in engine._group_threads
    clock.advance(6)
    provider.error_kind = "direct"
    failed = await engine.handle(event("明确问一句", mentioned=True, event_id="failed-direct"))
    assert failed["mode"] == "none" and failed["text"] == ""
    engine.acknowledge(failed["reply_id"], "sent", "95002")
    assert IDENTITY not in engine._group_threads
    provider.error_kind = None
    clock.advance(6)
    await start(engine, event_id="working", receipt="95003")
    clock.advance(6)
    provider.error_kind = "followup"
    silent = await engine.handle(event(event_id="failed-followup"))
    assert silent["mode"] == "none" and silent["text"] == ""
    count = len(calls(provider, "followup"))
    clock.advance(6)
    assert (await engine.handle(event(event_id="backoff")))["mode"] == "none"
    assert len(calls(provider, "followup")) == count

@pytest.mark.asyncio
async def test_followup_keeps_intervening_members_and_can_decline(rig):
    engine,settings,provider,clock=rig
    await start(engine)
    clock.advance(6)
    engine.groups.add(SCOPE,'20002','今晚谁来双排？','other-question',display_name='群友乙')
    provider.followup_reply={'continuation':False,'participate':False,'confidence':.1,'text':''}
    result=await engine.handle(event('我来，等我五分钟',event_id='answer-other'))
    assert result['mode']=='none' and result['reason']=='conversation_observed'
    assert calls(provider,'followup')==[]
    messages=plans(provider,'followup')[-1]['messages']
    recent=next(json.loads(m['content'].split('\n',1)[1])['recent_conversation'] for m in messages
                if '"recent_conversation"' in m['content'])
    assert any(r['message_id']=='other-question' for r in recent)
    assert '人类转向其他成员后' in messages[0]['content']
    assert IDENTITY not in engine._group_threads

@pytest.mark.asyncio
async def test_native_quote_is_in_final_message_even_outside_recent_window(rig):
    engine,settings,provider,clock=rig
    quote={'message_id':'1234','user_id':'10001','kind':'group','scope_id':'30001',
           'text':'那个位置可以先放一盏小灯。','sent_at':int(clock.now)-3600,'media_types':[]}
    result=await engine.handle(event('你说放哪里？',event_id='quote-now',reply_to_id='1234',quoted_message=quote))
    assert result['mode']=='text'
    current=json.loads(calls(provider,'direct')[-1]['messages'][-1]['content'].split('\n',1)[1])['current_message']
    assert current['quoted_message']['text']==quote['text']
    assert current['quote_status']=='resolved'
    assert current['has_images'] is False


def test_quote_cannot_cross_conversations_or_reply_ids(rig):
    from pydantic import ValidationError
    quote={'message_id':'1234','user_id':'10001','kind':'group','scope_id':'30002',
           'text':'别的群','sent_at':1}
    with pytest.raises(ValidationError):event(reply_to_id='1234',quoted_message=quote)
    quote['scope_id']='30001'
    with pytest.raises(ValidationError):event(reply_to_id='5678',quoted_message=quote)


def test_unavailable_quote_is_not_invented_and_optout_quote_is_filtered(rig):
    engine,_,_,clock=rig
    missing=engine._current_message(event(reply_to_id='1234'))
    assert missing['quote_status']=='unavailable' and 'quoted_message' not in missing
    quote={'message_id':'1234','user_id':'20002','kind':'group','scope_id':'30001','text':'已撤回旁听','sent_at':1}
    engine.groups.set_optout(SCOPE,'20002',True)
    current=engine._current_message(event(reply_to_id='1234',quoted_message=quote))
    assert 'quoted_message' not in current
