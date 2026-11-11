"""Planning failure, privacy and execution budgets through the real Engine."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from azusa_bot import conversation_config, group_planning, lore
from azusa_bot.core import Incoming
from azusa_bot.providers import ProviderError
from context_helpers import context as wire_context
from test_group_engine import rig, event, configure, SCOPE


class PlanningProvider:
    def __init__(self, *, plan_error=None, plan_change=None, speech=None):
        self.plan_error, self.plan_change = plan_error, plan_change
        self.speech = list(speech or [{'text': '这是这次公开讨论中的一句简短回应。', 'mode': 'text'}])
        self.calls, self.plan_calls, self.speech_calls = [], [], []
        self.wait_for_plan = False

    async def complete(self, messages, **kwargs):
        assert not kwargs.get('summary')
        data = wire_context(messages)
        self.calls.append(deepcopy(data))
        if data.get('task') == 'group_plan':
            self.plan_calls.append(deepcopy(data))
            if self.plan_error:
                raise self.plan_error
            if self.wait_for_plan:
                await asyncio.Event().wait()
            number = data['current_message']['message_id']
            plan = dict(action='reply', target='group', addressee_ids=[],
                        response_to_message_id=number, thread_anchor=number,
                        evidence_message_ids=[number], reason='public_contribution')
            answer = {'participation_plan': plan, 'confidence': .95,
                      'votes': {key: .95 for key in ('relevance', 'understanding', 'contribution', 'timing')}}
            return self.plan_change(answer, data) if self.plan_change else answer
        self.speech_calls.append(deepcopy(data))
        assert self.speech, 'Unplanned extra generation call'
        return self.speech.pop(0)


def execution(engine, *, models, tools):
    old = conversation_config.describe(engine.content_root)
    value = old['config'] | {'execution': {'max_model_calls': models, 'max_tool_rounds': tools}}
    conversation_config.save(engine.content_root, value, old['revision'])


@pytest.mark.parametrize('error', [ProviderError('deepseek_network_or_format'), RuntimeError('synthetic_provider_failure')])
async def test_planner_failure_is_quiet_and_never_starts_speech_or_reserves_a_reply(rig, error):
    engine, _, _, _ = rig
    model = engine.provider_override = PlanningProvider(plan_error=error)
    response = await engine.handle(event('今天的云很好看'))
    assert response['mode'] == 'none' and response['text'] == ''
    assert len(model.plan_calls) == 1 and not model.speech_calls
    assert engine.groups.budget_status(SCOPE)['day_used'] == 0
    assert not engine._group_pending


@pytest.mark.parametrize(('change', 'attempts'), [
    (lambda _answer, _data: {}, 1),
    (lambda _answer, _data: {'participation_plan': []}, 1),
    (lambda _answer, _data: ['not an object'], 1),
    (lambda answer, _data: answer | {'participation_plan': answer['participation_plan'] | {'evidence_message_ids': ['1', 'fabricated']}}, 1),
    (lambda answer, _data: answer | {'participation_plan': answer['participation_plan'] | {'thread_anchor': 'fabricated'}}, 1),
    (lambda answer, _data: answer | {'participation_plan': answer['participation_plan'] | {'response_to_message_id': 'fabricated'}}, 1),
    (lambda answer, _data: answer | {'participation_plan': answer['participation_plan'] | {'target': 'self', 'addressee_ids': ['99999']}}, 2),
])
async def test_invalid_planner_results_and_fabricated_evidence_fail_quiet(rig, change, attempts):
    engine, _, _, _ = rig
    model = engine.provider_override = PlanningProvider(plan_change=change)
    response = await engine.handle(event('今天的云很好看'))
    assert response['mode'] == 'none' and response['text'] == ''
    assert len(model.plan_calls) == attempts and not model.speech_calls
    assert engine.groups.budget_status(SCOPE)['day_used'] == 0
    assert engine.local_conversation.get(SCOPE, '1')['source'] == 'unresolved'
    assert all(row['role'] == 'user' for row in engine.groups.snapshot(SCOPE))


@pytest.mark.parametrize(('fields', 'attempts'), [
    ({'target': 'group', 'addressee_ids': ['20002', '20003']}, 2),
    ({'action': 'observe', 'target': 'unknown', 'response_to_message_id': None}, 1),
    ({'action': ['observe']}, 1),
    ({'target': {'group': True}}, 2),
])
async def test_rejected_plan_keeps_invalid_output_diagnostic(rig, fields, attempts):
    engine, _, _, _ = rig

    def malformed(answer, _data):
        answer['participation_plan'].update(fields)
        answer['mode'] = ['not_part_of_plan']
        return answer

    model = engine.provider_override = PlanningProvider(plan_change=malformed)
    result = await engine.handle(event('今天的云很好看', mentioned_user_ids=['20002', '20003']))
    assert result['reason'] == 'conversation_plan_invalid'
    assert len(model.plan_calls) == attempts and not model.speech_calls
    assert engine.local_conversation.get(SCOPE, '1')['needs_assessment']
    trace = engine.trajectories.list(kind='group_plan')['items'][0]
    assert trace['status'] == 'invalid_output' and trace['error_code'] == 'conversation_plan_invalid'


@pytest.mark.parametrize(('plan_fields', 'score_scale', 'reason'), [
    ({}, 1, 'low_confidence'),
    ({'response_to_message_id': None, 'thread_anchor': None}, 1, 'low_confidence'),
    ({'response_to_message_id': 'fabricated'}, 1, 'conversation_plan_invalid'),
    ({'action': 'reply', 'target': 'group'}, 1, 'conversation_plan_invalid'),
    ({}, 5, 'conversation_plan_invalid'),
])
async def test_low_vote_observation_does_not_require_reply_anchors(rig, plan_fields, score_scale, reason):
    engine, settings, _, _ = rig
    weights = {'relevance': 70, 'understanding': 80, 'contribution': 20, 'timing': 35}
    configure(engine, settings, group_vote_enabled=True, group_chat_confidence_threshold=.81,
              **{'group_vote_weight_' + key: weight for key, weight in weights.items()})
    scores = {'relevance': .15, 'understanding': .2, 'contribution': .1, 'timing': .2}

    def observe(_answer, data):
        return {
            'votes': {key: score * score_scale for key, score in scores.items()},
            'participation_plan': {
                'action': 'observe', 'target': 'unknown', 'addressee_ids': [],
                'evidence_message_ids': [data['current_message']['message_id']], 'reason': 'no_new_contribution',
                **plan_fields,
            },
        }

    model = engine.provider_override = PlanningProvider(plan_change=observe)
    result = await engine.handle(event('直播通知：订阅的直播间开播了'))
    assert result['mode'] == 'none' and result['reason'] == reason
    assert len(model.plan_calls) == 1 and not model.speech_calls and not engine._group_pending
    assert engine.local_conversation.get(SCOPE, '1')['needs_assessment']
    trace = engine.trajectories.list(kind='group_plan')['items'][0]
    assert trace['participation_vote']['score'] == pytest.approx(.1731707317 * score_scale)
    if reason == 'low_confidence':
        assert trace['status'] == 'low_confidence' and not trace['error_code']
        assert trace['participation_plan'] is None and trace['response_text'] is None
    else:
        assert trace['status'] == 'invalid_output' and trace['error_code'] == reason


async def test_planning_timeout_cancels_provider_and_leaves_no_speech_or_pending_send(rig, monkeypatch):
    engine, _, _, _ = rig
    model = engine.provider_override = PlanningProvider()
    model.wait_for_plan = True
    timeout = asyncio.timeout
    monkeypatch.setattr(group_planning, 'asyncio', SimpleNamespace(timeout=lambda _seconds: timeout(.005)))
    response = await engine.handle(event('今天的云很好看'))
    assert response['mode'] == 'none' and response['reason'] == 'conversation_plan_unavailable'
    assert len(model.plan_calls) == 1 and not model.speech_calls and not engine._group_pending
    records = engine.trajectories.list()['items']
    assert any(row['kind'] == 'group_plan' and row['status'] == 'interrupted' for row in records)


@pytest.mark.parametrize('kind', ['native', 'legacy', 'own_name'])
async def test_explicit_self_fast_path_does_not_depend_on_working_planner(rig, kind):
    engine, _, _, _ = rig
    engine.address_names_loader = lambda: ['小梓']
    model = engine.provider_override = PlanningProvider(plan_error=AssertionError('Planner must not run'))
    incoming = (event('小梓，你好') if kind == 'own_name' else
                event('请回答这个问题', mentioned_user_ids=['10001']) if kind == 'native' else
                event('请回答这个问题', mentioned=True))
    response = await engine.handle(incoming)
    assert response['mode'] == 'text' and not model.plan_calls and len(model.speech_calls) == 1
    assert engine.target_reply_current(response['reply_id'])


@pytest.mark.parametrize('own_name', ['阿梓', '灰泽满'])
async def test_named_joint_request_planner_can_reply_with_only_its_own_id(rig, own_name):
    engine, _, provider, _ = rig
    engine.address_names_loader = lambda: [own_name]

    def choose_self(data):
        assert data['own_names'] == [own_name]
        assert data['local_relation']['facts']['self_name_candidate']
        number = data['current_message']['message_id']
        return {'confidence':.05, 'participation_plan':{
            'action':'reply', 'target':'self', 'addressee_ids':[data['self_id']],
            'response_to_message_id':number, 'thread_anchor':number,
            'evidence_message_ids':[number], 'reason':'named_joint_request',
        }}

    provider.plan_answer = choose_self
    response = await engine.handle(event('阿梓和灰泽满说话来'))

    assert response['mode'] == 'text' and len(provider.plan_calls) == len(provider.calls) == 1
    policy = provider.plan_calls[0]['messages'][0]['content']
    assert '甲和乙说话来' in policy and 'addressee_ids=[self_id]' in policy
    assert '甲，和乙聊聊' in policy and '甲和乙昨天聊过吗' in policy
    plan = wire_context(provider.calls[0]['messages'])['participation_plan']
    assert plan['target'] == 'self' and plan['addressee_ids'] == ['10001']


async def test_opted_out_explicit_call_has_only_a_transient_plan_and_no_resident_relation(rig):
    engine, _, _, _ = rig
    engine.groups.set_optout(SCOPE, '20001', True)
    model = engine.provider_override = PlanningProvider(plan_error=AssertionError('Fast path expected'))
    response = await engine.handle(event('本轮独有的临时输入', mentioned_user_ids=['10001']))
    assert response['mode'] == 'text' and not model.plan_calls
    assert engine.local_conversation.get(SCOPE, '1') is None
    assert engine.target_reply_current(response['reply_id'])
    guard = engine._group_pending[response['reply_id']]['target_guard']
    assert guard['source'] == 'private_single_turn'
    assert '本轮独有的临时输入' not in repr(guard)
    engine.acknowledge(response['reply_id'], 'sent', '90001')
    assert engine.local_conversation.get(SCOPE, '1') is None
    assert not any(row['user_id'] == '20001' for row in engine.groups.snapshot(SCOPE))
    assert not engine.dialogue.get(SCOPE, '20001', ttl_seconds=300)


async def test_opted_out_ambiguous_turn_can_be_observed_without_retaining_planning_input(rig):
    engine, _, _, _ = rig
    engine.groups.set_optout(SCOPE, '20001', True)
    def decline(answer, data):
        return {'participation_plan': answer['participation_plan'] | {'action': 'observe', 'target': 'unknown'}}
    model = engine.provider_override = PlanningProvider(plan_change=decline)
    response = await engine.handle(event('刚才被提到的事情', mentioned=True))
    assert response['mode'] == 'none'
    assert len(model.plan_calls) == 1 and not model.speech_calls
    assert engine.local_conversation.get(SCOPE, '1') is None and not engine._group_pending


@pytest.mark.parametrize('transport', ['official', 'onebot_disabled_group'])
async def test_unsupported_group_paths_do_not_start_local_planning(rig, transport):
    engine, settings, _, _ = rig
    model = engine.provider_override = PlanningProvider(plan_error=AssertionError('Unsupported planning path'))
    if transport == 'official':
        configure(engine, settings, transport='official', qq_enabled=True,
                  app_id='official-app', allowed_groups=['30001'], onebot_self_id='')
        incoming = Incoming(transport='official', app_id='official-app', kind='group', scope_id='30001',
                            user_id='20001', event_id='1', text='请回答这个问题')
        scope = 'qq:official-app:group:30001'
    else:
        configure(engine, settings, group_chat_enabled=False)
        incoming = event('请回答这个问题', mentioned=True, unprompted=False)
        scope = SCOPE
    response = await engine.handle(incoming)
    assert response['mode'] == 'text' and len(model.speech_calls) == 1 and not model.plan_calls
    assert engine.local_conversation.get(scope, '1') is None


async def test_planning_and_tool_generation_consume_one_shared_model_budget(rig):
    engine, _, _, _ = rig
    execution(engine, models=3, tools=1)
    lore.import_documents(engine.content_root, [{'text': '合成星球：用于离线验证的虚构星球。', 'category': 'reference'}])
    model = engine.provider_override = PlanningProvider(speech=[
        {'knowledge_query': '合成星球', 'text': '', 'mode': 'text'},
        {'text': '资料里的合成星球是虚构的。', 'mode': 'text'},
    ])
    response = await engine.handle(event('群里的新话题是合成星球'))
    assert response['mode'] == 'text', (response, model.calls)
    assert len(model.plan_calls) == 1 and len(model.speech_calls) == 2
    assert model.speech_calls[-1]['knowledge_lookup']['status'] == 'found'
    records = engine.trajectories.list()['items']
    model_records = [row for row in records if row['kind'] in {'group_plan', 'group_decision'}]
    assert sorted(row['execution']['model_call'] for row in model_records) == [1, 2, 3]
    assert model_records[0]['execution']['tools'] == ['knowledge_lookup']
    assert all(row['execution']['max_model_calls'] == 3 for row in model_records)


async def test_one_model_call_budget_is_spent_by_planning_and_cannot_start_speech(rig):
    engine, _, _, _ = rig
    execution(engine, models=1, tools=0)
    model = engine.provider_override = PlanningProvider()
    response = await engine.handle(event('今天的云很好看'))
    assert response['mode'] == 'none' and response['text'] == ''
    assert len(model.plan_calls) == 1 and not model.speech_calls
    assert engine.groups.budget_status(SCOPE)['day_used'] == 0
    assert engine.local_conversation.get(SCOPE, '1')['source'] == 'assessment'
