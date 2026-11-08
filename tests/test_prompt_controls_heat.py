"""Editable instructions and event-driven local heat; entirely synthetic/offline."""
import asyncio
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from azusa_bot.app import create_app
from azusa_bot.prompts import DEFAULTS, describe, save, prompt, PromptConflict
from azusa_bot.group_chat import GroupConversation
from azusa_bot.settings import Settings
from azusa_bot.context_budget import fit_context
from azusa_bot.vision_prompt import visual_sources
from test_group_engine import rig, event, configure, model_calls, SCOPE


def test_prompt_overrides_are_profile_scoped_and_conflict_checked(tmp_path):
    a=tmp_path/'a';b=tmp_path/'b';settings=Settings()
    data=describe(a,settings)
    save(a,{'style':'我自定义的聊天风格','direct_header':'背景内容'},data['revision'])
    assert prompt(a,'style')=='我自定义的聊天风格'
    assert prompt(b,'style')==DEFAULTS['style']
    assert prompt(a,'direct_header')=='背景内容\n'
    with pytest.raises(PromptConflict):save(a,{'style':'旧页面'},data['revision'])
    save(a,{'style':None},describe(a,settings)['revision'])
    assert prompt(a,'style')==DEFAULTS['style']
    assert len(describe(a,settings)['blocks'])==len(DEFAULTS)


@pytest.mark.parametrize('changes',[{'unknown':'x'},{'style':12},{'style':'${unknown}'},{'group_header':'一行\n另一行'},{'style':'a'*12001}])
def test_invalid_prompts_rejected(tmp_path,changes):
    with pytest.raises(ValueError):save(tmp_path,changes,describe(tmp_path,Settings())['revision'])


@pytest.mark.asyncio
async def test_actual_model_uses_custom_prompts_and_headers_without_exposing_them_in_trace(rig):
    engine,settings,model,clock=rig
    changes={k:f'CUSTOM_{k}' for k in ('group','knowledge','expression','vision','output','style','persona_header','persona_footer','search_offline')}
    changes['group_header']='管理员自定义数据说明'
    save(engine.content_root,changes,describe(engine.content_root,settings)['revision'])
    reply=await engine.handle(event())
    assert reply['mode']=='text'
    messages=model_calls(model)[0]['messages']
    assert all(f'CUSTOM_{k}' in messages[0]['content'] for k in changes if k!='group_header')
    assert messages[1]['content'].startswith('管理员自定义数据说明\n{')
    trace=engine.trajectories.list()['items'][0]
    assert trace['prompt_revision']==describe(engine.content_root,settings)['revision']
    saved=engine.trajectories._db.execute('select refs from trajectories').fetchone()[0]
    assert '管理员自定义数据说明' not in saved


def seed_heat(engine,clock):
    for n in range(8):
        engine.groups.add(SCOPE,str(21000+n%3),f'讨论游戏第 {n} 个想法',str(900+n))
        clock.advance(1)


@pytest.mark.asyncio
async def test_heat_enters_during_normal_cooldown_then_uses_persistent_three_reply_cap(rig):
    engine,settings,model,clock=rig
    configure(engine,settings,group_heat_enabled=True,group_heat_require_topic=False,group_chat_cooldown_seconds=90,group_chat_decision_interval_seconds=60)
    seed_heat(engine,clock)
    initial=engine.groups.reserve(SCOPE,cooldown_seconds=0,hour_limit=8,day_limit=40)
    engine.groups.mark_sent(initial)
    for n in range(3):
        clock.advance(21)
        reply=await engine.handle(event(text=f'这个游戏挺有意思的 {n}',event_id=str(100+n)))
        assert reply['mode']=='text',reply
        envelope=json.loads(model_calls(model)[-1]['messages'][1]['content'].split('\n',1)[1])
        assert envelope['group_activity']['hot'] is True
    assert len(model_calls(model))==3
    clock.advance(21)
    reply=await engine.handle(event(event_id='104'))
    assert reply['mode']=='none' and len(model_calls(model))==3
    restarted=GroupConversation(engine.groups.path,clock=clock)
    assert restarted.heat(SCOPE,settings)['replies_used']==3
    restarted.close()


def test_heat_excludes_single_member_repeats_bots_optouts_and_expired_messages(rig):
    engine,settings,model,clock=rig
    settings.group_heat_enabled=True
    for n in range(15):engine.groups.add(SCOPE,'22000',f'我连发 {n}',str(n+1))
    assert not engine.groups.heat(SCOPE,settings)['hot']
    for n in range(9):engine.groups.add(SCOPE,str(23000+n%3),'复读复读',str(100+n))
    assert not engine.groups.heat(SCOPE,settings)['hot']
    for n in range(10):engine.groups.add(SCOPE,str(24000+n),f'机器人 {n}',str(200+n),role='assistant')
    assert not engine.groups.heat(SCOPE,settings)['hot']
    engine.groups.clear_scope(SCOPE);seed_heat(engine,clock)
    assert engine.groups.heat(SCOPE,settings)['eligible']
    engine.groups.set_optout(SCOPE,'21001',True)
    assert not engine.groups.heat(SCOPE,settings)['eligible']
    clock.advance(91)
    assert engine.groups.heat(SCOPE,settings)['messages']==0


@pytest.mark.asyncio
async def test_hot_discussion_new_other_member_does_not_discard_fresh_answer(rig):
    engine,settings,model,clock=rig
    configure(engine,settings,group_heat_enabled=True,group_heat_require_topic=False)
    seed_heat(engine,clock)
    model.entered=asyncio.Event();model.release=asyncio.Event()
    task=asyncio.create_task(engine.handle(event(event_id='123')))
    await model.entered.wait()
    engine.groups.add(SCOPE,'21001','大家接着聊呀','222')
    model.release.set()
    assert (await task)['mode']=='text'


@pytest.mark.asyncio
async def test_hot_discussion_still_respects_privacy_cancellation(rig):
    engine,settings,model,clock=rig
    configure(engine,settings,group_heat_enabled=True,group_heat_require_topic=False)
    seed_heat(engine,clock)
    model.entered=asyncio.Event();model.release=asyncio.Event()
    task=asyncio.create_task(engine.handle(event(event_id='123')))
    await model.entered.wait();engine.invalidate_groups();model.release.set()
    assert (await task)['mode']=='none'


def test_admin_prompt_auth_conflict_and_profile_switch(rig):
    engine,settings,model,clock=rig
    app=create_app(engine.root)
    headers={'Authorization':'Bearer '+(engine.root/'data/admin.token').read_text().strip(),'X-XZBot-Profile':'default'}
    with TestClient(app) as client:
        assert client.get('/admin/prompts').status_code in (401,403)
        data=client.get('/admin/prompts',headers=headers).json()
        payload={'revision':data['revision'],'changes':{'style':'仅默认角色'}}
        assert client.put('/admin/prompts',headers=headers,json=payload).status_code==200
        assert client.put('/admin/prompts',headers=headers,json=payload).status_code==409
        role=client.post('/admin/profiles',headers=headers,json={'name':'测试角色'}).json()['id']
        client.post(f'/admin/profiles/{role}/activate',headers=headers,json={})
        assert client.put('/admin/prompts',headers=headers,json=payload).status_code==409
        data=client.get('/admin/prompts',headers={**headers,'X-XZBot-Profile':role}).json()
        assert next(b['text'] for b in data['blocks'] if b['key']=='style')==DEFAULTS['style']


@pytest.mark.asyncio
async def test_hot_slow_answer_is_not_sent_after_topic_window(rig):
    engine,settings,model,clock=rig
    configure(engine,settings,group_heat_enabled=True,group_heat_require_topic=False)
    seed_heat(engine,clock)
    model.entered=asyncio.Event();model.release=asyncio.Event()
    task=asyncio.create_task(engine.handle(event(event_id='123')))
    await model.entered.wait();clock.advance(16);model.release.set()
    assert (await task)['mode']=='none'


def test_heat_does_not_revive_old_pending_messages(rig):
    engine,settings,model,clock=rig
    engine.groups.add(SCOPE,'22000','比较早之前的话','444')
    clock.advance(20);seed_heat(engine,clock)
    assert not engine.groups.recent_user_event(SCOPE,'22000','444')
