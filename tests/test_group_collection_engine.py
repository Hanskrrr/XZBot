"""Automatic member observation is independent of unsolicited bot replies."""
import json
import pytest

from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import ProviderError
from azusa_bot.settings import Settings, write_settings

SCOPE='onebot:10001:group:30001'


class Provider:
    def __init__(self): self.calls=[];self.fail=False
    async def complete(self,messages,*,summary=False):
        self.calls.append(summary)
        if self.fail: raise ProviderError('upstream_error')
        if summary:
            source=json.loads(messages[-1]['content'])[0]
            return {'memories':[{'text':'用户自述喜欢合成测试中的合作游戏。','source_ids':[source['id']]}]}
        return {'participate':False,'confidence':0,'text':''}


@pytest.fixture
def rig(tmp_path):
    (tmp_path/'content').mkdir();(tmp_path/'content/persona.md').write_text('用于离线测试的原创角色人设。')
    settings=Settings(transport='onebot',onebot_enabled=True,onebot_self_id='10001',
        onebot_allowed_groups=['30001','30002'],onebot_allowed_dm_users=['20001'],
        group_chat_enabled=False,group_chat_debounce_seconds=.01,group_chat_min_messages=1,
        auto_summary_every=4,min_interval_seconds=0,read_existing_dsh=False)
    write_settings(tmp_path,settings)
    engine=Engine(tmp_path);provider=Provider();engine.provider_override=provider
    engine.memory.set_group_collection(SCOPE,True)
    yield engine,settings,provider
    engine.memory.close();engine.groups.close()


def event(number,user='20001',text=None,**changes):
    return Incoming(**(dict(transport='onebot',app_id='10001',kind='group',scope_id='30001',
        user_id=user,event_id=str(number),text=text or f'合成测试：我喜欢合作游戏第{number}句',
        unprompted=True,mentioned=False,display_name='相同的可修改昵称')|changes))


@pytest.mark.asyncio
async def test_all_new_members_observed_independently_without_speaking_or_model(rig):
    engine,_,provider=rig
    for number,user in enumerate(['20001','20002','20003'],1):
        result=await engine.handle(event(number,user))
        assert result['mode']=='none' and result['reason']=='memory_observed'
    assert provider.calls==[] and engine.groups.snapshot(SCOPE)==[]
    for number,user in enumerate(['20001','20002','20003'],1):
        history=engine.memory.list_history(SCOPE,user)
        assert len(history)==1 and history[0]['event_id']==str(number)
    assert (await engine.handle(event(1)))['reason']=='duplicate_event'
    assert len(engine.memory.list_history(SCOPE,'20001'))==1


@pytest.mark.asyncio
async def test_silent_observation_is_summarized_by_background_queue(rig):
    engine,_,provider=rig
    for i in range(4): await engine.handle(event(i+1))
    assert provider.calls==[]
    result=await engine.summarize_due()
    assert result['saved']==1 and provider.calls==[True]
    assert len(engine.memory.list_memories(SCOPE,'20001'))==1
    assert engine.memory.list_memories(SCOPE,'20002')==[]


@pytest.mark.asyncio
async def test_group_defaults_do_not_override_member_withdrawal_or_leak_other_scopes(rig):
    engine,_,provider=rig
    await engine.handle(event(1))
    await engine.handle(event(2,text='/记忆 关闭',unprompted=False))
    engine.memory.set_group_collection(SCOPE,False);engine.memory.set_group_collection(SCOPE,True)
    await engine.handle(event(3))
    await engine.handle(event(4,'20002'))
    await engine.handle(event(5,'20003',scope_id='30002'))
    assert engine.memory.list_history(SCOPE,'20001')==[]
    assert len(engine.memory.list_history(SCOPE,'20002'))==1
    assert engine.memory.list_history('onebot:10001:group:30002','20003')==[]
    assert not engine.memory.consent('onebot:10001:dm:20002','20002')
    assert provider.calls==[]


@pytest.mark.asyncio
async def test_passive_filters_and_listener_optout_still_apply(rig):
    engine,_,provider=rig
    engine.groups.set_optout(SCOPE,'20002',True)
    events=[event(1,text='password=synthetic-test-secret'),event(2,'20002'),
        event(3,'10001'),event(4,scope_id='99999'),event(5,text='/unknown-synthetic-command')]
    for incoming in events: assert (await engine.handle(incoming))['mode']=='none'
    assert engine.memory.stats()['history']==0 and provider.calls==[]


@pytest.mark.asyncio
async def test_speaking_mode_cloud_failure_and_backoff_do_not_drop_member_observations(rig):
    engine,settings,provider=rig
    settings.group_chat_enabled=True;settings.provider_mode='cloud'
    write_settings(engine.root,settings);provider.fail=True
    for i,user in enumerate(['20001','20002','20003'],1):
        assert (await engine.handle(event(i,user)))['mode']=='none'
    assert provider.calls==[False]
    for user in ['20001','20002','20003']:
        assert len(engine.memory.list_history(SCOPE,user))==1


def test_privacy_command_explains_current_group_policy(rig):
    engine,_,_=rig
    message=engine.command(event(1),SCOPE,'/隐私',False)
    assert '本群已开启全员默认记忆' in message and '个人关闭设置优先' in message
    engine.memory.set_group_collection(SCOPE,False)
    assert '本范围未开启全群默认记忆' in engine.command(event(1),SCOPE,'/隐私',False)
