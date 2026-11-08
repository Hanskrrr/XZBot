"""Receipt-confirmed short context, independent of long-term memory and turn caps."""
import json
import pytest

from azusa_bot.dialogue import RecentDialogue, refers_back, requests_repeat
from azusa_bot.prompts import DEFAULTS, SCENES, describe, save
from test_group_followup import rig, event, start, configure, calls, SCOPE, IDENTITY


def context(provider):
    return json.loads(provider.calls[-1]['messages'][1]['content'].split('\n',1)[1])


def dm(text, event_id):
    return event(text, event_id=event_id, kind='dm', scope_id='20001', unprompted=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', ['sent', 'failed', 'uncertain', 'missing'])
async def test_private_context_does_not_require_memory_consent_but_requires_receipt(rig, receipt):
    engine, _, provider, clock = rig
    scope='onebot:10001:dm:20001'
    assert not engine.memory.consent(scope,'20001')
    first=await engine.handle(dm('给我说一句晚安','p1'))
    assert first['mode']=='text'
    if receipt!='missing': engine.acknowledge(first['reply_id'],receipt,'90001')
    clock.advance(6)
    second=await engine.handle(dm('再来一次','p2'))
    assert second['mode']=='text'
    rows=context(provider).get('confirmed_direct_dialogue',[])
    assert [r['text'] for r in rows]==(['给我说一句晚安',provider.direct_reply['text']] if receipt=='sent' else [])
    assert engine.memory.stats()['history']==0
    if rows:
        assert rows[-1]['mode']=='text'
        assert engine.trajectories.list()['items'][0]['counts']['dialogue']==2


@pytest.mark.asyncio
async def test_repeated_direct_mentions_keep_whole_turns_even_with_followup_disabled(rig):
    engine, settings, provider, clock=rig
    configure(engine,settings,group_followup_enabled=False)
    for i,text in enumerate(('讲个短笑话','换一个','再来一次')):
        clock.advance(6)
        result=await engine.handle(event(text,mentioned=True,event_id=f'd{i}'))
        assert result['mode']=='text'
        assert len(context(provider).get('confirmed_direct_dialogue',[]))==2*i
        engine.acknowledge(result['reply_id'],'sent',f'90{i}')
    assert IDENTITY not in engine._group_threads


@pytest.mark.asyncio
@pytest.mark.parametrize('ended_by', ['turn_cap','timer'])
async def test_explicit_repeat_reopens_recent_context_and_can_repeat_identical_answer(rig,ended_by):
    engine,settings,provider,clock=rig
    await start(engine)
    if ended_by=='turn_cap':engine._group_threads[IDENTITY]['turns']=settings.group_followup_max_turns
    else:clock.advance(settings.group_followup_seconds+1)
    clock.advance(6)
    provider.followup_reply['text']=provider.direct_reply['text']
    result=await engine.handle(event('再来一次',event_id='repeat'))
    assert result['mode']=='text' and result['text']==provider.direct_reply['text']
    assert calls(provider,'followup')
    assert [r['message_id'] for r in context(provider)['confirmed_direct_dialogue']]==['start','90001']


@pytest.mark.asyncio
async def test_normal_followup_still_blocks_accidental_duplicate(rig):
    engine,_,provider,clock=rig
    await start(engine);clock.advance(6)
    provider.followup_reply['text']=provider.direct_reply['text']
    result=await engine.handle(event('为什么这个输入不行',event_id='not-repeat'))
    assert result['reason']=='repetitive_reply'


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['expired','other_member','other_group','cleared'])
async def test_repeat_does_not_reopen_unrelated_or_expired_turns(rig,change):
    engine,settings,provider,clock=rig
    await start(engine);clock.advance(6)
    updates={}
    if change=='expired':clock.advance(settings.group_chat_context_minutes*60)
    if change=='other_member':updates['user_id']='20002'
    if change=='other_group':updates['scope_id']='30002'
    if change=='cleared':engine.clear_group_context(SCOPE)
    result=await engine.handle(event('再来一次',event_id='not-ours',**updates))
    assert result['mode']=='none' and not calls(provider,'followup')


@pytest.mark.asyncio
async def test_old_private_archive_is_not_silently_replayed(rig):
    engine,_,provider,clock=rig
    scope='onebot:10001:dm:20001'
    engine.memory.set_consent(scope,'20001',True)
    engine.memory.add_history(scope,'20001','user','很久以前的独立话题','ancient')
    result=await engine.handle(dm('再来一次','new'))
    assert result['mode']=='text'
    assert '很久以前的独立话题' not in str(provider.calls[-1]['messages'])


@pytest.mark.asyncio
async def test_reference_policy_is_editable_and_used_in_actual_request(rig):
    engine,settings,provider,clock=rig
    save(engine.content_root,{'dialogue':'CUSTOM_DIALOGUE_POLICY'},describe(engine.content_root,settings)['revision'])
    await engine.handle(dm('你好','p1'))
    assert 'CUSTOM_DIALOGUE_POLICY' in provider.calls[-1]['messages'][0]['content']
    assert all('dialogue' in SCENES[key] for key in ('直接回复','群聊接话','连续对话'))
    assert not any('AI' in text or '不是现实主播' in text for text in DEFAULTS.values())


def test_cache_retains_actual_voice_mode_and_drops_whole_expired_turns():
    now=[100.]
    cache=RecentDialogue(lambda:now[0])
    pending=dict(scope='g',user_id='u',self_id='bot',event_id='in',user_text='念一句晚安',user_at=90.,text='晚安啦',mode='voice')
    cache.add(pending,'out');cache.add(pending,'out')
    rows=cache.get('g','u',ttl_seconds=60)
    assert len(rows)==2 and rows[-1]['mode']=='voice'
    now[0]=151
    assert cache.get('g','u',ttl_seconds=60)==[]


def test_reference_cues_only_open_a_candidate_not_a_send_decision():
    assert refers_back('那再来一次') and refers_back('换一个呢')
    assert requests_repeat('用语音再说一遍')
    assert not requests_repeat('不要再说一遍了')
    assert not refers_back('今天出太阳了')
