"""Idle participation uses synthetic activity, clocks and providers only."""
import asyncio
from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient

from support_content import seed_content
from azusa_bot.app import create_app
from azusa_bot.core import Engine, Incoming
from azusa_bot.group_idle import in_hours
from azusa_bot.settings import Settings, write_settings
from context_helpers import context

SELF='10001'; GROUP='11001'; SCOPE=f'onebot:{SELF}:group:{GROUP}'

class Clock:
    now=datetime(2026,11,7,4,tzinfo=timezone.utc).timestamp()
    def __call__(self):return self.now
    def advance(self, seconds):self.now+=seconds

class Provider:
    def __init__(self):
        self.calls=[]; self.entered=None; self.release=None; self.fail=False
        self.plan_action='reply'
        self.plan_confidence=None
        self.answer={'text':'刚想到一个轻松的话题，最近有没有让人眼前一亮的小事。','mode':'text'}
    async def complete(self,messages,**kwargs):
        self.calls.append(messages)
        if self.entered:self.entered.set()
        if self.release:await self.release.wait()
        if self.fail:raise RuntimeError('synthetic failure')
        data=context(messages)
        if data.get('task')=='group_plan':
            return {'confidence':self.answer.get('confidence',.95) if self.plan_confidence is None else self.plan_confidence,
                'participation_plan':{'action':self.plan_action,'target':'group','addressee_ids':[],
                'evidence_message_ids':[row['message_id'] for row in data.get('recent_conversation',[])],
                'reason':'synthetic idle decision'}}
        return self.answer

@pytest.fixture
def rig(tmp_path):
    seed_content(tmp_path)
    settings=Settings(transport='onebot',onebot_enabled=True,onebot_self_id=SELF,
        onebot_allowed_groups=[GROUP,'30002'],group_chat_enabled=True,group_idle_enabled=True,
        group_idle_groups=[GROUP],group_chat_debounce_seconds=0,provider_mode='cloud',
        daily_cloud_requests=20,group_chat_hourly_limit=20,group_chat_daily_limit=40)
    write_settings(tmp_path,settings)
    engine=Engine(tmp_path);clock=Clock();engine.idle.clock=clock;engine.groups.clock=clock
    provider=Provider();engine.provider_override=provider
    yield engine,settings,clock,provider
    engine.memory.close();engine.groups.close()

def activity(n=1, **extra):
    return Incoming(transport='onebot',app_id=SELF,kind='group',scope_id=GROUP,
        user_id='20001',event_id=str(n),text='',unprompted=True,activity_only=True,**extra)

async def ready(rig,n=1):
    e,_,clock,_=rig
    result=await e.handle(activity(n));assert result['reason']=='activity_observed'
    clock.advance(1801)

async def test_one_decision_until_new_human_activity_and_single_ack(rig):
    e,_,clock,p=rig
    assert await e.idle.claim(SELF)==[]
    await ready(rig)
    items=await e.idle.claim(SELF);assert len(items)==1 and len(p.calls)==2
    item=items[0]
    assert await e.idle.claim(SELF)==[]
    assert not e.groups.snapshot(SCOPE)
    assert not e.idle.authorize(item['id'],'wrong',SELF)
    assert not e.idle.authorize(item['id'],'错误口令',SELF)
    assert not e.idle.ack(item['id'],item['claim_token'],SELF,[])['acknowledged']
    assert not e.idle.ack(item['id'],'错误口令',SELF,'failed')['acknowledged']
    assert e.idle.authorize(item['id'],item['claim_token'],SELF)
    assert not e.idle.authorize(item['id'],item['claim_token'],SELF)
    assert e.idle.ack(item['id'],item['claim_token'],SELF,'sent','-123')['acknowledged']
    assert e.groups.snapshot(SCOPE)[-1]['role']=='assistant'
    clock.advance(7200)
    assert await e.idle.claim(SELF)==[] and len(p.calls)==2
    await ready(rig,2)
    assert len(await e.idle.claim(SELF))==1 and len(p.calls)==4

@pytest.mark.parametrize('mode',['silent','failure','uncertain'])
async def test_silence_failure_and_uncertain_are_not_polled_into_repeat_calls(rig,mode):
    e,_,clock,p=rig
    if mode=='silent':p.plan_action='observe';p.answer={'text':'不可展示'}
    if mode=='failure':p.fail=True
    await ready(rig)
    items=await e.idle.claim(SELF)
    if mode=='uncertain':
        item=items[0];assert e.idle.authorize(item['id'],item['claim_token'],SELF)
        assert e.idle.ack(item['id'],item['claim_token'],SELF,'uncertain')['acknowledged']
    else:assert not items
    for _ in range(3):
        clock.advance(1801);assert await e.idle.claim(SELF)==[]
    assert len(p.calls)==(2 if mode=='uncertain' else 1) and not e.groups.snapshot(SCOPE)

async def test_daily_attempt_budget_and_private_memory_not_used(rig):
    e,settings,clock,p=rig
    settings.group_idle_daily_limit=2;write_settings(e.root,settings)
    e.memory.add_memory(SCOPE,'20001','PRIVATE_SYNTHETIC_MEMBER_NOTE',kind='personal',is_owner=True)
    p.plan_action='observe'
    for n in range(1,4):
        await ready(rig,n);await e.idle.claim(SELF)
    assert len(p.calls)==2
    assert 'PRIVATE_SYNTHETIC_MEMBER_NOTE' not in json.dumps(p.calls)
    assert e.idle.status()['groups'][0]['attempts_today']==2
    with e.memory._lock:
        rows=e.memory._db.execute("SELECT kind FROM trajectories").fetchall()
    assert len(rows)==2 and all(row['kind']=='group_plan' for row in rows)

async def test_optout_and_media_activity_postpone_without_retaining_text(rig):
    e,_,clock,p=rig
    e.groups.set_optout(SCOPE,'20001')
    await e.handle(activity(1));clock.advance(1790)
    await e.handle(activity(2));clock.advance(20)
    assert await e.idle.claim(SELF)==[]
    assert not e.groups.snapshot(SCOPE) and not p.calls
    clock.advance(1781)
    assert len(await e.idle.claim(SELF))==1

@pytest.mark.parametrize('change',['message','clear','settings','profile','power'])
async def test_changes_cancel_inflight_before_send(rig, change):
    e,settings,clock,p=rig
    p.entered=asyncio.Event();p.release=asyncio.Event()
    await ready(rig)
    task=asyncio.create_task(e.idle.claim(SELF));await asyncio.wait_for(p.entered.wait(),2)
    if change=='message':await e.handle(activity(2))
    elif change=='clear':e.clear_group_context(SCOPE)
    elif change=='settings':settings.group_idle_enabled=False;write_settings(e.root,settings);e.invalidate_groups()
    elif change=='profile':e._active_guard=lambda:False
    else:
        from azusa_bot.bot_control import BotPowerController
        BotPowerController(e.root,e.invalidate_groups)._write('stopped')
    p.release.set();assert await task==[]
    assert not e.idle.pending

async def test_new_activity_cancels_prepared_and_restart_requires_new_human(rig):
    e,_,clock,p=rig
    await ready(rig);item=(await e.idle.claim(SELF))[0]
    await e.handle(activity(2))
    assert not e.idle.authorize(item['id'],item['claim_token'],SELF)
    from azusa_bot.group_idle import IdleConversation
    e.idle=IdleConversation(e,clock=clock)
    clock.advance(1801)
    assert await e.idle.claim(SELF)==[]

@pytest.mark.parametrize('human_first', [False, True])
async def test_peer_activity_consumes_idle_chance_until_new_human(rig, human_first):
    e,_,clock,p=rig
    if human_first:
        await e.handle(activity(1))
    clock.advance(120)
    e.idle.peer_spoke(SCOPE)
    row=e.idle.status()['groups'][0]
    assert row['last_activity_at']==clock() and row['armed'] is False
    for _ in range(2):
        clock.advance(1801)
        assert await e.idle.claim(SELF)==[] and not p.calls
        e.idle.peer_spoke(SCOPE)
    await ready(rig,2)
    assert len(await e.idle.claim(SELF))==1 and len(p.calls)==2


async def test_peer_activity_cancels_prepared_idle_reply(rig):
    e,_,clock,p=rig
    await ready(rig)
    item=(await e.idle.claim(SELF))[0]
    clock.advance(1)
    e.idle.peer_spoke(SCOPE)
    assert not e.idle.pending
    assert not e.idle.authorize(item['id'],item['claim_token'],SELF)
    clock.advance(1801)
    assert await e.idle.claim(SELF)==[] and len(p.calls)==2
    assert not e.groups.snapshot(SCOPE)


async def test_peer_activity_cancels_inflight_idle_generation(rig):
    e,_,clock,p=rig
    p.entered=asyncio.Event();p.release=asyncio.Event()
    await ready(rig)
    task=asyncio.create_task(e.idle.claim(SELF))
    await asyncio.wait_for(p.entered.wait(),2)
    e.idle.peer_spoke(SCOPE)
    p.release.set()
    assert await task==[] and not e.idle.pending
    clock.advance(1801)
    assert await e.idle.claim(SELF)==[] and len(p.calls)==1


async def test_wrong_account_whitelist_hours_and_shared_budget_gate_before_model(rig):
    e,settings,clock,p=rig
    await ready(rig)
    assert await e.idle.claim('99999')==[]
    settings.group_idle_groups=[];write_settings(e.root,settings)
    assert await e.idle.claim(SELF)==[]
    settings.group_idle_groups=[GROUP];settings.group_idle_start_hour=20;settings.group_idle_end_hour=21;write_settings(e.root,settings)
    assert await e.idle.claim(SELF)==[]
    settings.group_idle_start_hour=0;settings.group_idle_end_hour=24;settings.group_chat_hourly_limit=1;write_settings(e.root,settings)
    assert e.groups.reserve(SCOPE,cooldown_seconds=0,hour_limit=1,day_limit=40)
    assert await e.idle.claim(SELF)==[] and not p.calls


def test_hours_support_overnight_and_equal_is_closed():
    settings=Settings(group_idle_start_hour=22,group_idle_end_hour=9)
    # 00:00 UTC = 08:00 China.
    now=datetime(2026,11,7,0,tzinfo=timezone.utc).timestamp()
    assert in_hours(settings,now)
    assert not in_hours(settings,now+7200)
    settings.group_idle_start_hour=9;settings.group_idle_end_hour=9
    assert not in_hours(settings,now)


def test_admin_and_bridge_routes_are_separately_authenticated(tmp_path):
    seed_content(tmp_path);write_settings(tmp_path,Settings(fish_reference_id='synthetic',fish_model_override=True))
    app=create_app(tmp_path);c=TestClient(app)
    admin={'Authorization':'Bearer '+(tmp_path/'data/admin.token').read_text().strip()}
    bridge={'Authorization':'Bearer '+(tmp_path/'data/bridge.token').read_text().strip()}
    assert c.get('/admin/groups/idle/status',headers=bridge).status_code==401
    assert c.get('/admin/groups/idle/status',headers=admin).json()['enabled'] is False
    assert c.post('/bridge/idle/claim',json={'self_id':SELF}).status_code==401
    assert c.post('/bridge/idle/claim',headers=bridge,json={'self_id':SELF}).json()=={'messages':[]}

async def test_interleaved_duplicate_event_does_not_rearm(rig):
    e,_,clock,p=rig
    p.plan_action='observe'
    await e.handle(activity(1));await e.handle(activity(2));clock.advance(1801)
    assert await e.idle.claim(SELF)==[] and len(p.calls)==1
    result=await e.handle(activity(1));assert result['reason']=='duplicate_event'
    clock.advance(1801)
    assert await e.idle.claim(SELF)==[] and len(p.calls)==1
    assert e.idle.status()['groups'][0]['armed'] is False

async def test_waiting_for_group_lock_rechecks_budget_before_paid_call(rig):
    e,settings,clock,p=rig
    settings.group_chat_hourly_limit=1;write_settings(e.root,settings)
    await ready(rig)
    lock=e.groups.lock(SCOPE)
    await lock.acquire()
    task=asyncio.create_task(e.idle.claim(SELF))
    await asyncio.sleep(.02)
    assert e.groups.reserve(SCOPE,cooldown_seconds=0,hour_limit=1,day_limit=40)
    lock.release()
    assert await task==[] and not p.calls


def test_selected_groups_visible_before_activity_and_while_disabled(rig):
    e,settings,_,_=rig
    row=e.idle.status()['groups'][0]
    assert row=={'group_id':GROUP,'last_activity_at':0.,'armed':False,'attempts_today':0}
    settings.group_idle_enabled=False;write_settings(e.root,settings)
    data=e.idle.status()
    assert not data['enabled'] and data['groups']==[row]


async def test_idle_generation_uses_the_plan_without_requiring_participation_scores(rig):
    e,_,_,p=rig
    # These obsolete generation flags cannot override the validated planner.
    p.answer={'text':'换个轻松的话题吧。','mode':'text','participate':False,'confidence':0}
    p.plan_confidence=.95
    await ready(rig)
    item,=await e.idle.claim(SELF)
    assert item['text']==p.answer['text']
    assert context(p.calls[0])['task']=='group_plan'
    generated=context(p.calls[1])
    assert generated['participation_plan']['action']=='reply'
    assert 'current_message' not in generated
    assert e.trajectories.list(kind='group_idle')['items'][0]['reply_contract']['required']==['text','mode']


async def test_auto_idle_cannot_send_generation_without_a_validated_plan(rig):
    e,_,_,p=rig
    async def unplanned(*args,**kwargs):return {'text':'不能发出的正文','mode':'text','participate':True,'confidence':1}
    e._complete_traced=unplanned
    await ready(rig)
    assert await e.idle.claim(SELF)==[]
    assert not e.idle.pending and not p.calls


@pytest.mark.parametrize('activity_kind',['human','peer','self'])
async def test_authorized_send_receipt_survives_new_room_activity_without_rearming(rig,activity_kind):
    e,_,_,_=rig
    await ready(rig)
    item,=await e.idle.claim(SELF)
    assert e.idle.authorize(item['id'],item['claim_token'],SELF)
    if activity_kind=='human':await e.handle(activity(2))
    elif activity_kind=='peer':e.idle.peer_spoke(SCOPE)
    else:e.idle.bot_spoke(SCOPE)
    assert not e.idle.authorize(item['id'],item['claim_token'],SELF)
    assert e.idle.pending[item['id']]['plan_cancelled'] is True
    assert e.idle.ack(item['id'],item['claim_token'],SELF,'sent','90001')['acknowledged']
    rows=e.groups.snapshot(SCOPE)
    assert len(rows)==1 and rows[0]['message_id']=='90001' and rows[0]['text']==item['text']
    assert rows[0]['role']=='assistant' and 'response_to_message_id' not in rows[0]
    assert not e._group_threads and not e.dialogue.turns
    assert not e.idle.ack(item['id'],item['claim_token'],SELF,'sent','90001')['acknowledged']


@pytest.mark.parametrize('change',['clear','profile','power','expired'])
async def test_authorized_receipt_never_revives_invalidated_context(rig,change):
    e,_,clock,_=rig
    await ready(rig)
    item,=await e.idle.claim(SELF)
    assert e.idle.authorize(item['id'],item['claim_token'],SELF)
    if change=='clear':e.clear_group_context(SCOPE)
    elif change=='profile':e._active_guard=lambda:False
    elif change=='expired':clock.advance(241)
    else:
        from azusa_bot.bot_control import BotPowerController
        BotPowerController(e.root,e.invalidate_groups)._write('stopped')
    e.idle.ack(item['id'],item['claim_token'],SELF,'sent','90001')
    assert not e.groups.snapshot(SCOPE)
