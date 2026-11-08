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

SELF='10001'; GROUP='11001'; SCOPE=f'onebot:{SELF}:group:{GROUP}'

class Clock:
    now=datetime(2026,11,7,4,tzinfo=timezone.utc).timestamp()
    def __call__(self):return self.now
    def advance(self, seconds):self.now+=seconds

class Provider:
    def __init__(self):
        self.calls=[]; self.entered=None; self.release=None; self.fail=False
        self.answer={'participate':True,'confidence':.95,'text':'刚想到一个轻松的话题，最近有没有让人眼前一亮的小事。'}
    async def complete(self,messages,**kwargs):
        self.calls.append(messages)
        if self.entered:self.entered.set()
        if self.release:await self.release.wait()
        if self.fail:raise RuntimeError('synthetic failure')
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
    items=await e.idle.claim(SELF);assert len(items)==1 and len(p.calls)==1
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
    assert await e.idle.claim(SELF)==[] and len(p.calls)==1
    await ready(rig,2)
    assert len(await e.idle.claim(SELF))==1 and len(p.calls)==2

@pytest.mark.parametrize('mode',['silent','failure','uncertain'])
async def test_silence_failure_and_uncertain_are_not_polled_into_repeat_calls(rig,mode):
    e,_,clock,p=rig
    if mode=='silent':p.answer={'participate':False,'text':'不可展示'}
    if mode=='failure':p.fail=True
    await ready(rig)
    items=await e.idle.claim(SELF)
    if mode=='uncertain':
        item=items[0];assert e.idle.authorize(item['id'],item['claim_token'],SELF)
        assert e.idle.ack(item['id'],item['claim_token'],SELF,'uncertain')['acknowledged']
    else:assert not items
    for _ in range(3):
        clock.advance(1801);assert await e.idle.claim(SELF)==[]
    assert len(p.calls)==1 and not e.groups.snapshot(SCOPE)

async def test_daily_attempt_budget_and_private_memory_not_used(rig):
    e,settings,clock,p=rig
    settings.group_idle_daily_limit=2;write_settings(e.root,settings)
    e.memory.add_memory(SCOPE,'20001','PRIVATE_SYNTHETIC_MEMBER_NOTE',kind='personal',is_owner=True)
    p.answer={'participate':False,'text':''}
    for n in range(1,4):
        await ready(rig,n);await e.idle.claim(SELF)
    assert len(p.calls)==2
    assert 'PRIVATE_SYNTHETIC_MEMBER_NOTE' not in json.dumps(p.calls)
    assert e.idle.status()['groups'][0]['attempts_today']==2
    with e.memory._lock:
        rows=e.memory._db.execute("SELECT kind FROM trajectories").fetchall()
    assert len(rows)==2 and all(row['kind']=='group_idle' for row in rows)

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
    p.answer={'participate':False,'text':''}
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
