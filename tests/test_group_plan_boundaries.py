"""Plans preserve factual repair, one request budget and truthful audit status."""
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from azusa_bot import activity_guard, participation_vote
from azusa_bot.settings import Settings
from context_helpers import context
from test_activity_guard import bind, live, IdleModel
from test_group_engine import SCOPE, configure, rig, event
from test_group_followup import configure as configure_followup, event as followup_event
from test_group_followup import rig as followup_rig, start as start_followup
from test_group_idle import rig as idle_rig, ready, SELF
from test_group_planning import PlanningProvider, execution
from test_group_topic import setup


@pytest.mark.parametrize('kind', ['group_decision', 'followup', 'group_idle'])
def test_valid_plan_checks_facts_without_legacy_generation_scores(kind):
    checks=[]
    engine=SimpleNamespace(content_root=None,
        _group_target_context=ContextVar('test_plan',default={'action':'reply'}),
        _group_target_current=lambda _plan:True,
        trajectories=SimpleNamespace(record_activity_check=lambda _id,result:checks.append(result)))
    answer={'text':'我刚下播了。','mode':'text'}
    kwargs={'kind':kind,'refs':{'role_activity':live(),'_trace_id':1}}
    result=activity_guard.assess(engine,answer,Settings(),kwargs)
    assert result['status']=='conflict' and result['reason']=='state_mismatch'
    assert checks==[result]
    # An absent or revoked plan is not permission to revive an old silent reply.
    engine._group_target_current=lambda _plan:False
    assert activity_guard.assess(engine,answer,Settings(),kwargs) is None
    assert len(checks)==1


async def test_planned_group_generation_repairs_text_only_activity_conflict(rig):
    engine,_,_,_=rig
    bind(engine)
    model=engine.provider_override=PlanningProvider(speech=[
        {'text':'我刚下播了。','mode':'text'},
        {'text':'我正在直播，抽空过来看看。','mode':'text'},
    ])
    response=await engine.handle(event('大家聊聊最近的生活'))
    assert response['text']=='我正在直播，抽空过来看看。'
    assert len(model.plan_calls)==1 and len(model.speech_calls)==2
    assert model.speech_calls[-1]['activity_correction']['expected']=='live'
    records=engine.trajectories.list(kind='group_decision')['items']
    assert records[-1]['status']=='activity_conflict'
    assert records[0]['status']=='success'


async def test_automatic_idle_repairs_text_only_activity_conflict(idle_rig):
    engine,_,_,_=idle_rig
    bind(engine)
    model=engine.provider_override=IdleModel([
        {'text':'我刚下播了。','mode':'text'},
        {'text':'我正在直播，抽空过来看看。','mode':'text'},
    ])
    await ready(idle_rig)
    items=await engine.idle.claim(SELF)
    assert len(items)==1 and items[0]['text']=='我正在直播，抽空过来看看。'
    assert len(model.calls)==3
    assert context(model.calls[-1])['activity_correction']['expected']=='live'


@pytest.mark.parametrize('max_calls', [1,2])
async def test_explicit_fast_call_and_topic_use_the_same_budget(rig,max_calls):
    engine,_,model,_=setup(rig)
    execution(engine,models=max_calls,tools=0)
    response=await engine.handle(event('请回答这个问题',event_id='1001',mentioned_user_ids=['10001']))
    assert not model.plan_calls
    assert model.stages==(['topic'] if max_calls==1 else ['topic','generation'])
    assert response['mode']==('none' if max_calls==1 else 'text')
    called=[row for row in reversed(engine.trajectories.list()['items']) if row['provider_called']]
    assert [row['execution']['model_call'] for row in called]==list(range(1,max_calls+1))
    assert all(row['execution']['max_model_calls']==max_calls for row in called)


async def test_idle_fabricated_evidence_has_invalid_audit_status(idle_rig):
    engine,_,_,_=idle_rig
    calls=[]
    class InvalidEvidence:
        async def complete(self,messages,**_kwargs):
            calls.append(messages)
            assert context(messages)['task']=='group_plan'
            return {'confidence':.95,'participation_plan':{
                'action':'reply','addressee_ids':[],
                'response_to_message_id':None,'thread_anchor':None,
                'evidence_message_ids':['not_received'],'reason':'synthetic_idle'}}
    engine.provider_override=InvalidEvidence()
    await ready(idle_rig)
    assert await engine.idle.claim(SELF)==[] and len(calls)==1
    record=engine.trajectories.list(kind='group_plan')['items'][0]
    assert record['status']=='invalid_output'
    assert record['error_code']=='conversation_plan_invalid'
    assert record['plan_failure_reason']=='evidence_invalid'
    assert record['response_preview_state']=='planning_only'
    assert record['confidence_state']=='valid' and record['confidence']==.95
    assert not engine._group_pending


@pytest.mark.parametrize('case', ['confirmed_followup', 'other_member', 'missing_reply_evidence', 'ordinary_group'])
async def test_signed_followup_only_repairs_the_speaker_in_a_confirmed_exchange(followup_rig, case):
    engine, settings, provider, clock = followup_rig
    question, sent_reply, current = '-301', '-901', '-302'
    configure_followup(engine, settings, group_vote_enabled=True, group_chat_confidence_threshold=.81)
    await start_followup(engine, event_id=question, receipt=sent_reply)
    if case == 'other_member':
        await engine.handle(followup_event('另一位成员的背景消息', user_id='20002', event_id='-401'))
    if case == 'ordinary_group':
        configure_followup(engine, settings, group_followup_enabled=False,
                           group_chat_min_messages=1, group_chat_cooldown_seconds=15)
    clock.advance(31)

    complete = provider.complete
    async def reversed_addressee(messages, **kwargs):
        answer = await complete(messages, **kwargs)
        data = context(messages)
        if data.get('task') == 'group_plan':
            assert {question, sent_reply, current} <= set(data['local_relation']['permitted_evidence_message_ids'])
            speaker = data['current_message']['user_id']
            evidence = [question, current] if case == 'missing_reply_evidence' else [question, sent_reply, current]
            answer['votes'] = dict.fromkeys(participation_vote.CRITERIA, .95)
            answer['participation_plan'].update(action='reply', target='self',
                addressee_ids=['20002' if case == 'other_member' else speaker],
                response_to_message_id=current, thread_anchor=question, evidence_message_ids=evidence)
        return answer

    provider.complete = reversed_addressee
    response = await engine.handle(followup_event('今天你必须选一个', event_id=current))
    traces = engine.trajectories.list(kind='group_plan')['items']
    trace = traces[0]
    assert len(traces) == (1 if case == 'confirmed_followup' else 2)
    assert trace['trigger'] == ('group_observation' if case == 'ordinary_group' else 'followup')
    assert trace['confidence'] == pytest.approx(.95) and trace['participation_vote']['passed']
    assert {question, sent_reply, current} <= {row.get('message_id') for row in trace['input_messages']['items']}
    if case != 'confirmed_followup':
        assert response['reason'] == trace['error_code'] == 'conversation_plan_invalid'
        assert trace['plan_failure_reason'] == 'self_addressee_mismatch'
        assert response['mode'] == 'none' and len(provider.calls) == 1
        return
    assert response['mode'] == 'text' and trace['status'] == 'success' and len(provider.calls) == 2
    plan = context(provider.calls[-1]['messages'])['participation_plan']
    assert plan['target'] == 'self' and plan['addressee_ids'] == ['10001']
    assert plan['response_to_message_id'] == current and plan['thread_anchor'] == question
    assert set(plan['evidence_message_ids']) == {question, sent_reply, current}


@pytest.mark.parametrize(('score', 'effective_action'), [(.95, 'reply'), (.2, 'observe')])
async def test_cross_thread_plan_reports_anchor_mismatch_and_effective_action(rig, score, effective_action):
    engine, settings, _, _ = rig
    configure(engine, settings, group_chat_min_messages=3,
              group_vote_enabled=True, group_chat_confidence_threshold=.81)
    response_to, anchor, current = '-501', '-502', '-503'

    def cross_thread(answer, data):
        assert set(data['local_relation']['permitted_evidence_message_ids']) == {response_to, anchor, current}
        answer['votes'] = dict.fromkeys(participation_vote.CRITERIA, score)
        answer['participation_plan'].update(response_to_message_id=response_to, thread_anchor=anchor,
                                           evidence_message_ids=[response_to, anchor, current])
        return answer

    model = engine.provider_override = PlanningProvider(plan_change=cross_thread)
    first = await engine.handle(event('今天下午拍照', user_id='20002', event_id=response_to))
    second = await engine.handle(event('今晚准备吃拉面', user_id='20003', event_id=anchor))
    assert first['reason'] == second['reason'] == 'collecting_context'
    result = await engine.handle(event('今天的安排还挺多', event_id=current))

    assert result['mode'] == 'none' and result['reason'] == 'conversation_plan_invalid'
    assert len(model.plan_calls) == 2 and not model.speech_calls
    assert engine.groups.budget_status(SCOPE)['day_used'] == 0 and not engine._group_pending
    trace, first_trace = engine.trajectories.list(kind='group_plan')['items']
    assert trace['related_id'] == first_trace['id']
    assert first_trace['plan_failure_reason'] == 'thread_anchor_mismatch'
    assert trace['status'] == 'invalid_output' and trace['error_code'] == 'conversation_plan_invalid'
    assert trace['plan_failure_reason'] == 'thread_anchor_mismatch'
    assert trace['participation_plan']['action'] == 'reply'
    assert trace['effective_action'] == effective_action
    assert trace['participation_vote']['passed'] is (effective_action == 'reply')


@pytest.mark.parametrize('conflict', [False, True])
async def test_plan_does_not_retry_after_a_new_local_reply(rig, conflict):
    engine, _, _, _ = rig

    def advance_thread(answer, data):
        current = data['current_message']['message_id']
        engine.local_conversation.observe(SCOPE, {
            'message_id':'-602', 'user_id':'20002', 'text':'我来接一下这句', 'reply_to_id':current,
        }, self_id=data['self_id'])
        if conflict:
            answer['participation_plan'].update(action='observe', target='unknown', addressee_ids=[data['self_id']])
        return answer

    model = engine.provider_override = PlanningProvider(plan_change=advance_thread)
    result = await engine.handle(event('今天想聊聊摄影', event_id='-601'))

    assert result['mode'] == 'none' and result['reason'] == 'conversation_plan_invalid'
    assert len(model.plan_calls) == 1 and not model.speech_calls
    trace, = engine.trajectories.list(kind='group_plan')['items']
    assert trace['status'] == 'invalid_output'
    assert trace['plan_failure_reason'] == ('target_addressee_mismatch' if conflict else 'thread_version_changed')
