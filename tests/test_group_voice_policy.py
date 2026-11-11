"""One per-group synthesis budget covers every reply route, with no cloud or QQ calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
from azusa_bot.core import Ledger
from azusa_bot.group_voice import GroupVoice
from azusa_bot.group_policies import create_router, save_policy, revision, read
from azusa_bot.providers import ProviderError, mock_group_plan
from azusa_bot.settings import Settings, write_settings
from test_group_voice import rig, event, SCOPE


def policy(e, **changes):
    save_policy(e,SCOPE,changes,revision(read(e.root).get(SCOPE,{})))


@pytest.mark.parametrize('route',['direct','command','named','passive','followup','handoff','idle_manual','idle_auto'])
async def test_off_covers_every_route_without_synthesis(rig,route):
    e,p,s=rig
    if route in ('followup','handoff'):
        p.answer['mode']='text'
        answer=await e.handle(event(mentioned=True));e.acknowledge(answer['reply_id'],'sent','90001')
        with e.groups.db() as db:db.execute('UPDATE group_replies SET at=at-10')
    # Raw override preserves the existing candidate to isolate voice gating.
    from azusa_bot.settings import private_write
    private_write(e.root/'config/groups.json',json.dumps({SCOPE:{'group_voice_mode':'off'}}))
    p.answer.update(mode='voice',text='好呀，已经听见你说的啦。')
    if route=='idle_manual':
        e.idle.queue_manual(SCOPE,'a'*32);result,=await e.idle.claim_manual('10001')
    elif route=='idle_auto':
        s.group_idle_enabled=True;s.group_idle_groups=['30001'];s.group_idle_start_hour=0;s.group_idle_end_hour=24
        write_settings(e.root,s)
        e.idle.observe(event(activity_only=True,text=''),SCOPE)
        e.idle.clock=lambda:time.time()+1900
        result,=await e.idle.claim('10001')
    else:
        if route=='passive':
            s.group_chat_min_messages=1;write_settings(e.root,s)
        kwargs={'event_id':'2'}
        if route in ('direct','command'):kwargs['mentioned']=True
        if route=='handoff':
            kwargs['user_id']='20002'
            def public_handoff(data):
                answer=mock_group_plan(data)
                answer['participation_plan'].update(action='reply',target='self',addressee_ids=['10001'],
                    response_to_message_id='90001',thread_anchor='1',evidence_message_ids=['1','90001','2'])
                return answer
            p.plan_answer=public_handoff
        text={'direct':'你好','command':'/语音 在吗','named':'阿梓，在吗','passive':'聊什么好呢','followup':'下一步呢','handoff':'你说的什么意思'}[route]
        result=await e.handle(event(text,**kwargs))
    assert result['mode']=='text' and p.speech==[]
    assert e.group_voice.status(SCOPE,e.settings(SCOPE))['used']==0


async def test_limited_falls_back_and_other_groups_private_chats_stay_independent(rig):
    e,p,s=rig
    s.onebot_allowed_groups.append('30002');s.onebot_allowed_dm_users=['20001'];write_settings(e.root,s)
    policy(e,group_voice_mode='limited',group_voice_daily_limit=1)
    first=await e.handle(event(mentioned=True))
    assert first['mode']=='voice'
    p.answer['text']='再跟你说一句晚安。'
    second=await e.handle(event('/语音 再说一句',mentioned=True,event_id='2'))
    assert second['mode']=='text' and len(p.speech)==1
    assert (await e.handle(event(mentioned=True,scope_id='30002',event_id='3')))['mode']=='voice'
    assert (await e.handle(event(kind='dm',scope_id='20001',mentioned=True,unprompted=False,event_id='4')))['mode']=='voice'
    assert e.group_voice.status(SCOPE,e.settings(SCOPE))['remaining']==0
    assert e.group_voice.status('onebot:10001:group:30002',e.settings())['used']==1
    assert e.group_voice.status('onebot:10001:dm:20001',s)['used']==0


async def test_failure_counts_but_pre_synthesis_cloud_budget_does_not(rig):
    e,p,s=rig;policy(e,group_voice_mode='limited',group_voice_daily_limit=1)
    p.error='synthetic_tts_failure'
    assert (await e.handle(event(mentioned=True)))['mode']=='text'
    assert e.group_voice.status(SCOPE,e.settings(SCOPE))['used']==1
    assert (await e.handle(event(mentioned=True,event_id='2')))['mode']=='text'
    assert len(p.speech)==1
    # Use a clean group with a single model request remaining, no TTS budget.
    s.provider_mode='cloud';s.daily_cloud_requests=1;s.onebot_allowed_groups.append('30002');write_settings(e.root,s)
    assert (await e.handle(event(mentioned=True,scope_id='30002',event_id='3')))['mode']=='text'
    assert e.group_voice.status('onebot:10001:group:30002',s)['used']==0
    assert len(p.speech)==1


async def test_revocation_during_synthesis_removes_audio(rig):
    e,p,s=rig;p.entered=asyncio.Event();p.release=asyncio.Event()
    task=asyncio.create_task(e.handle(event(mentioned=True)))
    await asyncio.wait_for(p.entered.wait(),2)
    policy(e,group_voice_mode='off');p.release.set()
    assert (await task)['mode']=='none'
    assert p.generated and not p.generated[0].exists()
    assert e.group_voice.status(SCOPE,e.settings(SCOPE))['used']==1


def test_rolling_atomic_budget_survives_new_instance_and_mode_switch(tmp_path):
    ledger=Ledger(tmp_path/'account.db');now=[100000.]
    v=GroupVoice(ledger,clock=lambda:now[0]);s=Settings()
    assert s.group_voice_mode=='auto'
    # Unlimited is really unlimited, and recorded to prevent toggling from resetting usage.
    for _ in range(12):assert v.reserve(SCOPE,s)
    limited=s.model_copy(update={'group_voice_mode':'limited','group_voice_daily_limit':2})
    fresh=GroupVoice(Ledger(ledger.path),clock=lambda:now[0])
    assert fresh.reserve(SCOPE,limited) is None
    assert fresh.status(SCOPE,limited)['used']==12
    now[0]+=86400
    assert fresh.status(SCOPE,limited)['used']==0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:fresh.reserve(SCOPE,limited),range(12)))
    assert sum(x is not None for x in results)==2
    assert fresh.reserve('onebot:10002:group:30001',limited)
    assert fresh.reserve('onebot:10001:group:30002',limited)
    assert fresh.status(SCOPE,limited)['next_slot_at']==now[0]+86400
    fresh.release(next(x for x in results if x))
    assert fresh.status(SCOPE,limited)['remaining']==1


def test_admin_payload_and_invalid_voice_options(rig):
    e,p,s=rig;app=FastAPI();app.include_router(create_router(SimpleNamespace(current=e)))
    with TestClient(app) as client:
        data=client.get('/admin/group-settings',params={'scope':SCOPE}).json()
        assert data['voice']['mode']=='auto' and data['voice']['limit'] is None
        for changes in ({'group_voice_mode':'invalid'},{'group_voice_daily_limit':0},{'group_voice_daily_limit':True}):
            assert client.put('/admin/group-settings',json={'scope':SCOPE,'revision':data['revision'],'settings':changes}).status_code==422
        changed=client.put('/admin/group-settings',json={'scope':SCOPE,'revision':data['revision'],'settings':{'group_voice_mode':'off'}}).json()
        assert changed['voice']['mode']=='off' and not e.settings(SCOPE).voice_enabled
        assert e.settings().voice_enabled
