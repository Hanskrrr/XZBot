"""Recent public group turns can be continued by a different member, not by keyword fiat."""
import asyncio
import pytest
from test_group_followup import rig, event, configure, calls, start, SCOPE
from context_helpers import context, dialogue
from azusa_bot.prompts import describe, save


async def open_turn(rig):
    e, _, _, clock = rig
    await start(e)
    clock.advance(6)


async def test_another_member_gets_semantic_judgment_with_public_context_and_trace(rig):
    e, s, p, clock = rig
    # Same member number in a DM must never leak into the group's handoff.
    e.dialogue.add(dict(scope='onebot:10001:dm:20001',user_id='20001',user_at=clock(),
        event_id='private1',user_text='PRIVATE_ONLY',self_id='10001',text='PRIVATE_REPLY'), '80001')
    await open_turn(rig)
    save(e.content_root, {'group_handoff':'${character_name}：自定义群友承接判断'}, describe(e.content_root,s)['revision'])
    result=await e.handle(event('你说的输入是指什么？',user_id='20002',event_id='new-member'))
    assert result['mode']=='text'
    assert len(calls(p,'followup'))==1 and not calls(p,'passive')
    messages=calls(p,'followup')[0]['messages'];data=context(messages)
    assert '自定义群友承接判断' in messages[0]['content']
    assert 'PRIVATE_' not in str(messages)
    assert dialogue(messages)==[]  # No inheritance of the other member's paired dialogue.
    assert {r['message_id'] for r in data['recent_conversation']} >= {'start','90001'}
    assert 'new-member' in str(messages[-1]) and messages[-1]['role']=='user'
    hint=e.trajectories.list()['items'][0]['group_handoff']
    assert hint['anchor_message_id']=='90001' and hint['anchor_age_seconds']==6
    assert data['group_handoff']==hint
    e.acknowledge(result['reply_id'],'sent','90002')
    thread=e._active_thread(SCOPE,'20002',s)
    assert thread and 'handoff' not in thread
    clock.advance(6);p.followup_reply=dict(p.followup_reply,text='对呀，把它代进去看看。')
    assert (await e.handle(event('那下一步呢',user_id='20002',event_id='next')))['mode']=='text'
    assert len(calls(p,'followup'))==2


@pytest.mark.parametrize('condition',['no_ack','expired','too_many','disabled','other_group','other_mention','no_cue','own_cap'])
async def test_no_spurious_cross_member_entry(rig,condition):
    e,s,p,clock=rig
    if condition=='no_ack':
        await e.handle(event('解释一下',mentioned=True,event_id='start'))
    else:await start(e)
    clock.advance(61 if condition=='expired' else 6)
    if condition=='too_many':
        for n in range(6):e.groups.add(SCOPE,'20003','另一件事情',f'other-{n}')
    if condition=='disabled':configure(e,s,group_followup_handoff_enabled=False)
    if condition=='own_cap':e._group_threads[(SCOPE,'20001')]['turns']=s.group_followup_max_turns
    kwargs=dict(user_id='20001' if condition=='own_cap' else '20002',event_id='candidate')
    if condition=='other_group':kwargs['scope_id']='30002'
    if condition=='other_mention':kwargs['other_mentioned']=True
    result=await e.handle(event('今天好热' if condition=='no_cue' else '你觉得呢',**kwargs))
    assert result['mode']=='none'
    assert not calls(p,'followup')


async def test_pronoun_can_refer_to_another_member_and_not_reopen_repeatedly(rig):
    e,s,p,clock=rig;await open_turn(rig)
    e.groups.add(SCOPE,'20003','我下周去旅游','other-question')
    p.followup_reply={'participate':False,'continuation':False,'confidence':.98,'text':''}
    assert (await e.handle(event('你去哪',user_id='20002',event_id='1')))['reason']=='followup_not_for_bot'
    assert len(calls(p,'followup'))==1
    clock.advance(6)
    assert (await e.handle(event('你哪天去',user_id='20002',event_id='2')))['mode']=='none'
    assert len(calls(p,'followup'))==1


async def test_handoff_still_honors_vote_threshold_and_privacy_cancellation(rig):
    e,s,p,clock=rig;await open_turn(rig)
    configure(e,s,group_vote_enabled=True)
    p.followup_reply=dict(p.followup_reply,votes={'relevance':.9,'understanding':.2,'contribution':.7,'timing':.8})
    result=await e.handle(event('你刚才是什么意思',user_id='20002',event_id='low'))
    assert result['mode']=='none' and result['reason']=='followup_low_confidence'
    assert e.trajectories.list()['items'][0]['participation_vote']
    configure(e,s,group_vote_enabled=False)
    p.entered=asyncio.Event();p.release=asyncio.Event()
    clock.advance(6)
    task=asyncio.create_task(e.handle(event('你说的呢',user_id='20003',event_id='pending')))
    await asyncio.wait_for(p.entered.wait(),2)
    e.clear_group_context(SCOPE)
    p.release.set()
    assert (await task)['mode']=='none'
    assert not e._group_handoffs and not e._group_threads
