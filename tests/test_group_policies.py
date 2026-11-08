"""Synthetic tests: group isolation, real-send clocks and admin-only browsing."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from azusa_bot.group_policies import effective, curve, create_router
from azusa_bot.settings import Settings, private_write
from azusa_bot.group_chat import GroupConversation
from azusa_bot.memory import MemoryStore
from test_group_engine import rig, event, configure, SCOPE

OTHER='onebot:10001:group:30002'

def override(engine, values):
    private_write(engine.root/'config/groups.json',json.dumps(values))


def test_overrides_isolate_account_and_group_and_preserve_legacy_idle(rig):
    e,s,_,_=rig
    configure(e,s,group_idle_enabled=True,group_idle_groups=['30001'])
    assert e.settings(SCOPE).group_idle_enabled
    assert not e.settings(OTHER).group_idle_enabled
    override(e,{SCOPE:{'group_chat_enabled':False,'group_idle_enabled':False},OTHER:{'group_idle_enabled':True}})
    assert not e.settings(SCOPE).group_chat_enabled
    assert e.settings(OTHER).group_chat_enabled
    assert e.settings(OTHER).group_idle_groups==['30002']
    assert e.settings('onebot:99999:group:30001').group_chat_enabled
    assert e.settings('onebot:10001:dm:30001').group_chat_enabled


def test_admin_cas_and_reject_privilege_fields(rig):
    e,*_=rig;app=FastAPI();app.include_router(create_router(SimpleNamespace(current=e)))
    with TestClient(app) as c:
        a=c.get('/admin/group-settings',params={'scope':SCOPE}).json()
        b=c.get('/admin/group-settings',params={'scope':OTHER}).json()
        payload={'scope':SCOPE,'revision':a['revision'],'settings':{'group_chat_enabled':False}}
        assert c.put('/admin/group-settings',json=payload).status_code==200
        assert c.put('/admin/group-settings',json=payload).status_code==409
        assert c.put('/admin/group-settings',json={'scope':OTHER,'revision':b['revision'],'settings':{'group_chat_hourly_limit':20}}).status_code==200
        a=c.get('/admin/group-settings',params={'scope':SCOPE}).json()
        assert not a['settings']['group_chat_enabled']
        assert c.put('/admin/group-settings',json={'scope':SCOPE,'revision':a['revision'],'settings':{'onebot_owners':[]}}).status_code==400
        assert c.get('/admin/group-settings',params={'scope':'onebot:999:group:30001'}).status_code==400
        assert c.put('/admin/group-settings',json={'scope':SCOPE,'revision':a['revision'],'settings':{'group_confidence_decay':'linear','group_confidence_floor':.9,'group_chat_confidence_threshold':.8}}).status_code==422


@pytest.mark.parametrize('mode,half',[('fixed',.8),('linear',.7),('slow_start',.75)])
def test_curve_grace_monotone_floor(mode,half):
    s=Settings(group_chat_confidence_threshold=.8,group_confidence_floor=.6,group_confidence_decay=mode,
               group_confidence_grace_minutes=10,group_confidence_decay_minutes=100)
    assert curve(s,600)['effective']==.8
    assert curve(s,3600)['effective']==half
    expected=.8 if mode=='fixed' else .6
    assert curve(s,1e9)['effective']==expected
    values=[curve(s,t)['effective'] for t in range(0,8000,100)]
    assert values==sorted(values,reverse=True)


def test_context_limits_do_not_shrink_other_groups(rig):
    e,*_=rig
    e.groups.configure_scope(SCOPE,5,60);e.groups.configure_scope(OTHER,30,600)
    for n in range(10):
        e.groups.add(SCOPE,'20001',f'内容 {n}',str(n))
        e.groups.add(OTHER,'20001',f'内容 {n}',str(n))
    assert len(e.groups.snapshot(SCOPE))==5
    assert len(e.groups.snapshot(OTHER))==10
    e.groups.clock.advance(61)
    assert e.groups.snapshot(SCOPE)==[]
    assert len(e.groups.snapshot(OTHER,ttl_seconds=600))==10


@pytest.mark.asyncio
async def test_silence_decays_only_unsolicited_and_trace_records_actual_threshold(rig):
    e,s,p,clock=rig
    configure(e,s,group_confidence_decay='slow_start',group_confidence_floor=.6,group_confidence_grace_minutes=0,group_confidence_decay_minutes=10)
    e.groups.silence_seconds(SCOPE);clock.advance(600)
    p.answer['confidence']=.7
    reply=await e.handle(event())
    assert reply['mode']=='text'
    trace=e.trajectories.list()['items'][0]
    assert trace['confidence_policy']['effective']==.6
    assert trace['confidence_threshold']==.6
    e.acknowledge(reply['reply_id'],'failed')
    assert e.confidence_policy(SCOPE,s)['effective']==.6
    clock.advance(20)
    reply=await e.handle(event(event_id='2'))
    e.acknowledge(reply['reply_id'],'sent','12345')
    assert e.confidence_policy(SCOPE,s)['effective']==.8
    fresh=GroupConversation(e.groups.path,clock=clock)
    clock.advance(300)
    assert fresh.silence_seconds(SCOPE)==300
    assert fresh.silence_seconds(OTHER)==0
    fresh.close()


@pytest.mark.asyncio
async def test_other_group_change_does_not_cancel_paid_reply_but_own_change_does(rig):
    e,s,p,clock=rig;p.entered=asyncio.Event();p.release=asyncio.Event()
    task=asyncio.create_task(e.handle(event()))
    await p.entered.wait();e.clear_group_context(OTHER);p.release.set()
    assert (await task)['mode']=='text'
    clock.advance(20);p.entered.clear();p.release.clear()
    task=asyncio.create_task(e.handle(event(event_id='2')))
    await p.entered.wait();e.clear_group_context(SCOPE);p.release.set()
    assert (await task)['mode']=='none'


def test_history_browser_cannot_leak_other_scopes_and_paginate(rig):
    e,*_=rig;m=e.memory
    for scope in [SCOPE,OTHER]:
        m.set_group_collection(scope,True)
        for n in range(5):m.add_history(scope,str(20001+n%2),'user',f'{scope} text {n}',str(n))
        m.add_memory(scope,'20001','共同记忆',kind='group',is_owner=True)
        m.add_memory(scope,'20001','成员记忆')
    first=m.browse_scope(SCOPE,limit=3)
    assert len(first['items'])==3 and all(x['scope']==SCOPE for x in first['items'])
    second=m.browse_scope(SCOPE,limit=3,before=first['next_before'])
    assert len(second['items'])==2 and second['next_before'] is None
    personal=m.browse_scope(SCOPE,user='20002')['items']
    assert len(personal)==2 and all(x['user_id']=='20002' for x in personal)
    assert len(m.browse_scope(SCOPE,kind='memories')['items'])==2
    assert len(m.browse_scope(SCOPE,kind='memories',user='20002')['items'])==1
    with pytest.raises(ValueError):m.browse_scope('')


def test_topic_budget_is_independent_per_group(rig):
    e,s,_,clock=rig;s.group_topic_daily_limit=1
    assert e.ledger.reserve_topic(SCOPE,s) is None
    clock.advance(70)
    assert e.ledger.reserve_topic(SCOPE,s)=='daily_limit'
    assert e.ledger.reserve_topic(OTHER,s) is None


@pytest.mark.asyncio
async def test_named_and_followup_do_not_lower_correctness_gate(rig):
    e,s,p,clock=rig
    configure(e,s,group_confidence_decay='linear',group_confidence_floor=.5,group_confidence_grace_minutes=0,group_confidence_decay_minutes=1)
    e.address_names_loader=lambda:['小z'];e.groups.silence_seconds(SCOPE);clock.advance(1000)
    p.answer['confidence']=.7
    reply=await e.handle(event('小z 你在吗'))
    assert reply['reason']=='low_confidence'
    assert e.trajectories.list()['items'][0]['confidence_threshold']==.8


def test_admin_read_does_not_start_silence_clock(rig):
    e,s,_,clock=rig
    e.confidence_policy(SCOPE,s);clock.advance(500)
    assert e.confidence_policy(SCOPE,s)['silence_seconds']==0


def test_browse_router_is_protected(tmp_path):
    from azusa_bot.app import create_app
    app=create_app(tmp_path)
    with TestClient(app) as c:
        for url in ['/admin/group-settings','/admin/memory/browse?scope=synthetic']:
            assert c.get(url).status_code in (401,403)


@pytest.mark.asyncio
async def test_idle_override_works_when_global_disabled(tmp_path):
    from test_group_idle import seed_content, Clock, Provider, activity, SELF, GROUP, SCOPE as ISCOPE
    from azusa_bot.core import Engine
    from azusa_bot.settings import write_settings
    seed_content(tmp_path)
    settings=Settings(transport='onebot',onebot_enabled=True,onebot_self_id=SELF,onebot_allowed_groups=[GROUP,'30002'],group_chat_enabled=False,group_idle_enabled=False)
    write_settings(tmp_path,settings);e=Engine(tmp_path);clock=Clock();e.idle.clock=e.groups.clock=clock;e.provider_override=Provider()
    try:
        override(e,{ISCOPE:{'group_chat_enabled':True,'group_idle_enabled':True,'group_confidence_decay':'linear','group_confidence_floor':.6,'group_confidence_grace_minutes':0,'group_confidence_decay_minutes':10}})
        e.provider_override.answer['confidence']=.7
        await e.handle(activity());clock.advance(1801)
        items=await e.idle.claim(SELF)
        assert len(items)==1
        assert e.trajectories.list()['items'][0]['confidence_threshold']==.6
        assert e.idle.status()['enabled']
    finally:e.groups.close();e.memory.close()


def test_title_catalog_is_reference_retrievable_without_full_loading(tmp_path):
    from pathlib import Path
    from azusa_bot import lore
    if not (Path(__file__).resolve().parents[1]/'examples/knowledge/xing10086/import.json').exists():pytest.skip("Optional locally imported corpus is not distributed with source")
    pack=json.loads((Path(__file__).parents[1]/'examples/knowledge/xing10086/import.json').read_text())
    source=json.loads((Path(__file__).parents[1]/'examples/knowledge/xing10086/titles-20261107.json').read_text())
    assert len(source['items'])==1060 and len({r['bvid'] for r in source['items']})==1060
    doc=pack['documents'][0]
    assert doc['category']=='reference' and not doc['always_include']
    assert all('阿梓' in r['title'] and r['uploader']=='XING10086' for r in source['items'])
    lore.import_documents(tmp_path,pack['documents'])
    for query in ['pvp大师给阿梓指路','梓禁城','不要逼我玩别的了']:
        results=lore.for_prompt(tmp_path,query)
        assert any('XING10086' in r['title'] for r in results)
        assert any('https://www.bilibili.com/video/' in r['text'] for r in results)
    assert all(r['category']=='reference' for r in lore.for_prompt(tmp_path,'梓禁城'))


@pytest.mark.asyncio
async def test_identical_group_save_does_not_cancel_inflight_reply_and_real_change_has_reason(rig):
    from azusa_bot.group_policies import save_policy, revision, read
    e,s,p,clock=rig;p.entered=asyncio.Event();p.release=asyncio.Event()
    task=asyncio.create_task(e.handle(event()))
    await p.entered.wait()
    assert not save_policy(e,SCOPE,{'group_chat_hourly_limit':e.settings(SCOPE).group_chat_hourly_limit},revision(read(e.root).get(SCOPE,{})))
    p.release.set();assert (await task)['mode']=='text'
    clock.advance(20);p.entered.clear();p.release.clear()
    task=asyncio.create_task(e.handle(event(event_id='changed')))
    await p.entered.wait()
    assert save_policy(e,SCOPE,{'group_chat_hourly_limit':13},revision(read(e.root).get(SCOPE,{})))
    p.release.set();assert (await task)['mode']=='none'
    trace=e.trajectories.list()['items'][0]
    assert trace['status']=='interrupted' and trace['error_code']=='privacy_context_changed'
    assert trace['cancellation_reason']=='group_settings_changed'
    assert '参与设置已更新' in trace['cancellation_message']
    # A later erased source must not be attributed to this older settings edit.
    assert e.context_change_reason(SCOPE,e.generation(SCOPE))=='source_changed'
