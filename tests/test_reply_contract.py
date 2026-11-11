"""Regressions for effective group controls at the actual provider boundary."""
import json
import pytest
from azusa_bot.reply_contract import build, control_errors
from azusa_bot.settings import Settings, private_write
from azusa_bot.prompts import describe, save
from azusa_bot import conversation_config, lore
from context_helpers import context as wire_context
from test_context_engine import used as cloud_calls
from test_group_engine import rig, event, configure, SCOPE, OTHER
from test_group_followup import rig as followup_rig, start, event as follow_event
from test_participation_vote import votes
from azusa_bot.providers import ProviderError, mock_group_plan
from azusa_bot.trajectory_admin import _call_purpose


def contract(messages):
    return json.loads([m['content'] for m in messages[1:] if m['role']=='system'][-1].rsplit('\n',1)[1])


def planning_examples(instructions):
    return [json.loads(line) for line in instructions.splitlines() if line.startswith('{')]


@pytest.mark.parametrize('voting', [True, False])
@pytest.mark.parametrize('idle', [True, False])
def test_planning_contract_matches_its_complete_json_example(voting, idle):
    from azusa_bot.reply_contract import planning_instructions

    schema = build(Settings(group_vote_enabled=voting), 'group_plan', idle_plan=idle)
    current = None if idle else '123'
    instructions = planning_instructions(schema, current)
    *partial_examples, example = planning_examples(instructions)
    assert set(example) == {'participation_plan', 'votes' if voting else 'confidence'} == set(schema['required'])
    plan = example['participation_plan']
    plan_schema = schema['properties']['participation_plan']
    assert set(plan) == set(plan_schema['required'])
    assert plan['action'] == 'observe' and plan['target'] == ('group' if idle else 'unknown')
    assert plan['addressee_ids'] == []
    for key in ('response_to_message_id', 'thread_anchor'):
        assert plan[key] == current
        assert plan_schema['properties'][key]['type'] == ('null' if idle else 'string')
    assert plan['evidence_message_ids'] == ([] if idle else [current])
    if idle:
        assert partial_examples == []
    else:
        assert partial_examples == [
            {'action':'observe', 'target':'others', 'addressee_ids':['<乙的账号ID>']},
            {'action':'reply', 'target':'self', 'addressee_ids':['<本轮self_id>']},
        ]
        assert 'action 和 target 分开判断' in instructions and '尖括号内是占位符' in instructions


@pytest.mark.parametrize('kind',['reply','group_decision','followup','group_idle'])
@pytest.mark.parametrize('voting',[True,False])
def test_required_fields_match_route_and_enabled_features(kind,voting):
    value=build(Settings(group_vote_enabled=voting,voice_enabled=True,voice_authorized=True),kind,tools=['web_search'])
    required=value['required']
    assert ('votes' in required)==(voting and kind!='reply')
    assert ('continuation' in required)==(kind=='followup')
    assert ('confidence' in required)==(not voting and kind!='reply')
    assert 'voice' in value['properties']['mode']['enum']
    assert ('web_search' in value['properties'])==(kind!='group_idle')
    assert 'threshold' not in str(value) and 'weight' not in str(value)


@pytest.mark.asyncio
async def test_scope_saved_controls_reach_prompt_and_trace_with_examples_and_long_background(rig):
    e,s,p,clock=rig
    private_write(e.root/'config/groups.json',json.dumps({SCOPE:{'group_vote_enabled':True,'group_vote_weight_understanding':70,'group_heat_enabled':True}}))
    configure(e,s,context_window_tokens=300000)
    lore.import_documents(e.content_root,[{'name':'synthetic-long.md','text':'LONG_BACKGROUND_END 测试背景内容。'*2000,'category':'canon','always_include':True}])
    old=conversation_config.describe(e.content_root)
    conversation_config.save(e.content_root,old['config']|{'begin_dialogs':['晚安','嗯，晚安啦。']},old['revision'])
    save(e.content_root,{'reply_contract':'EDITABLE_REQUIRED_FIELDS'},describe(e.content_root,s)['revision'])
    p.answer['votes']=votes(.95)
    reply=await e.handle(event('合成测试问题'))
    assert reply['mode']=='text'
    messages=p.calls[-1]['messages'];actual=contract(messages)
    assert actual['required']==['text','mode']
    planning=contract(p.plan_calls[-1]['messages'])
    assert 'votes' in planning['required'] and 'confidence' not in planning['required']
    plan_instructions = p.plan_calls[-1]['messages'][2]['content'].rsplit('\n', 1)[0]
    assert 'EDITABLE_REQUIRED_FIELDS' not in plan_instructions
    *partial_examples, example = planning_examples(plan_instructions)
    assert {(item['action'], item['target']) for item in partial_examples} == {('observe','others'),('reply','self')}
    assert example['participation_plan']['response_to_message_id'] == '1'
    assert set(example) == {'participation_plan', 'votes'}
    assert 'EDITABLE_REQUIRED_FIELDS' in messages[-2]['content']
    assert messages[3]['content'].count('LONG_BACKGROUND_END')==2000
    assert 'current_message' in messages[-1]['content']
    trace=e.trajectories.list()['items'][0]
    assert trace['effective_group_settings']['group_vote_weight_understanding']==70
    assert trace['reply_contract']==actual
    assert not e.settings(OTHER).group_vote_enabled
    assert trace['request_layout']['order'][-2:]==['reply_contract','current_message']


@pytest.mark.asyncio
async def test_missing_votes_report_invalid_output_without_an_extra_call_or_fake_scores(rig):
    e,s,p,_=rig;configure(e,s,group_vote_enabled=True)
    p.answer.update(confidence=.99,participate=True)
    def missing_votes(data):
        answer=mock_group_plan(data);answer.pop('votes')
        return answer
    p.plan_answer=missing_votes
    result=await e.handle(event())
    trace=e.trajectories.list()['items'][0]
    assert result['reason']=='conversation_plan_invalid' and not p.calls and len(p.plan_calls)==1
    assert trace['kind']=='group_plan' and trace['status']=='invalid_output'
    assert trace['error_code']=='conversation_plan_invalid'
    assert trace['participation_vote']['score'] is None
    assert trace['response_text'] is None


@pytest.mark.parametrize('code', ['deepseek_output_not_json', 'deepseek_empty_content'])
async def test_deepseek_plan_format_retry_has_its_own_trace_and_shares_the_turn_budget(rig, code):
    engine, settings, provider, _ = rig
    configure(engine, settings, provider_mode='cloud', daily_cloud_requests=3)

    def respond(data):
        if len(provider.plan_calls) == 1:
            raise ProviderError(code, usage={'completion_tokens':12})
        return mock_group_plan(data)

    provider.plan_answer = respond
    result = await engine.handle(event('大家今晚想玩游戏吗'))
    assert result['mode'] == 'text' and len(provider.plan_calls) == 2 and len(provider.calls) == 1
    assert '上次输出未能解析' in provider.plan_calls[-1]['messages'][0]['content']
    speech, retry, first = engine.trajectories.list()['items']
    assert first['error_code'] == code and first['usage']['completion_tokens'] == 12
    assert retry['related_id'] == first['id'] and retry['status'] == 'success'
    assert speech['related_id'] == retry['id']
    assert [row['execution']['model_call'] for row in (first, retry, speech)] == [1, 2, 3]
    assert cloud_calls(engine) == 3


@pytest.mark.parametrize(('quota', 'max_calls', 'expected_calls'), [(1, 4, 1), (4, 2, 1), (4, 4, 2)])
async def test_deepseek_plan_format_retry_stops_at_limits_or_repeated_failure(rig, quota, max_calls, expected_calls):
    engine, settings, provider, _ = rig
    configure(engine, settings, provider_mode='cloud', daily_cloud_requests=quota)
    old = conversation_config.describe(engine.content_root)
    conversation_config.save(engine.content_root, old['config'] | {
        'execution': {'max_model_calls':max_calls, 'max_tool_rounds':0},
    }, old['revision'])

    def fail(_data):
        raise ProviderError('deepseek_output_not_json')

    provider.plan_answer = fail
    result = await engine.handle(event())
    assert result['mode'] == 'none' and len(provider.plan_calls) == expected_calls and not provider.calls
    assert cloud_calls(engine) == expected_calls


@pytest.mark.parametrize(('case', 'max_calls', 'attempts', 'reason'), [
    ('corrected', 4, 2, 'conversation_observed'),
    ('repeated_conflict', 4, 2, 'conversation_plan_invalid'),
    ('reserve_generation', 2, 1, 'conversation_plan_invalid'),
    ('format_already_retried', 4, 2, 'conversation_plan_invalid'),
])
async def test_current_message_plan_retries_target_addressee_mismatch_once(rig, case, max_calls, attempts, reason):
    engine, settings, provider, _ = rig
    configure(engine, settings, provider_mode='cloud', daily_cloud_requests=4)
    old = conversation_config.describe(engine.content_root)
    conversation_config.save(engine.content_root, old['config'] | {
        'execution': {'max_model_calls':max_calls, 'max_tool_rounds':0},
    }, old['revision'])

    def respond(data):
        attempt = len(provider.plan_calls)
        if case == 'format_already_retried' and attempt == 1:
            raise ProviderError('deepseek_output_not_json')
        answer = mock_group_plan(data)
        target = 'others' if case == 'corrected' and attempt == 2 else 'unknown'
        answer['participation_plan'].update(action='observe', target=target, addressee_ids=['20002'],
                                           reason='synthetic_candidate_reason')
        return answer

    provider.plan_answer = respond
    result = await engine.handle(event('可以发一下刚才说的那张图吗？', event_id='-510', mentioned_user_ids=['20002']))

    assert result['mode'] == 'none' and result['reason'] == reason
    assert len(provider.plan_calls) == attempts and not provider.calls
    assert cloud_calls(engine) == attempts
    hint_marker = '上次参与计划未通过本地校验（target_addressee_mismatch）。'
    assert hint_marker not in provider.plan_calls[0]['messages'][0]['content']
    rows = list(reversed(engine.trajectories.list(kind='group_plan')['items']))
    assert [row['execution']['model_call'] for row in rows] == list(range(1, attempts + 1))
    assert [row['context_budget']['attempt'] for row in rows] == list(range(1, attempts + 1))
    if case == 'format_already_retried':
        assert rows[0]['status'] == 'failed' and rows[0]['error_code'] == 'deepseek_output_not_json'
    else:
        assert rows[0]['status'] == 'invalid_output' and rows[0]['plan_failure_reason'] == 'target_addressee_mismatch'
    if attempts == 2:
        retry_prompt = provider.plan_calls[1]['messages'][0]['content']
        if case == 'format_already_retried':
            assert '上次输出未能解析' in retry_prompt and hint_marker not in retry_prompt
        else:
            _, marker, guidance = retry_prompt.partition(hint_marker)
            assert marker == hint_marker and all(field in guidance for field in ('target', 'addressee_ids', 'unknown'))
            assert 'synthetic_candidate_reason' not in retry_prompt
        assert rows[1]['related_id'] == rows[0]['id']
        assert rows[1]['input_messages'] == rows[0]['input_messages']
    if case == 'corrected':
        assert rows[-1]['status'] == 'success' and rows[-1]['participation_plan']['target'] == 'others'
    else:
        assert rows[-1]['status'] == 'invalid_output' and rows[-1]['plan_failure_reason'] == 'target_addressee_mismatch'


async def test_reply_approved_by_semantic_retry_uses_the_reserved_generation_call(rig):
    engine, settings, provider, _ = rig
    configure(engine, settings, provider_mode='cloud', daily_cloud_requests=3)
    old = conversation_config.describe(engine.content_root)
    conversation_config.save(engine.content_root, old['config'] | {
        'execution': {'max_model_calls':3, 'max_tool_rounds':0},
    }, old['revision'])

    def respond(data):
        answer = mock_group_plan(data)
        if len(provider.plan_calls) == 1:
            answer['participation_plan'].update(action='observe', target='unknown', addressee_ids=[data['self_id']])
        else:
            answer['participation_plan'].update(action='reply', target='self', addressee_ids=[data['self_id']])
        return answer

    provider.plan_answer = respond
    result = await engine.handle(event('能再解释一句吗？', event_id='-511', mentioned_user_ids=['10001', '20002']))

    assert result['mode'] == 'text' and len(provider.plan_calls) == 2 and len(provider.calls) == 1
    speech, retry, first = engine.trajectories.list()['items']
    assert first['plan_failure_reason'] == 'target_addressee_mismatch'
    assert retry['status'] == 'success' and retry['related_id'] == first['id']
    assert _call_purpose(retry, first) == 'plan_validation_retry'
    assert speech['related_id'] == speech['planning_trace_id'] == retry['id']
    assert [row['execution']['model_call'] for row in (first, retry, speech)] == [1, 2, 3]
    plan = wire_context(provider.calls[0]['messages'])['participation_plan']
    assert plan['action'] == 'reply' and plan['target'] == 'self' and plan['addressee_ids'] == ['10001']
    assert cloud_calls(engine) == 3


@pytest.mark.asyncio
async def test_followup_custom_header_survives_while_plan_owns_continuation(followup_rig):
    e,s,p,clock=followup_rig
    save(e.content_root,{'followup_header':'CUSTOM_CONTINUATION_HEADER'},describe(e.content_root,s)['revision'])
    await start(e);clock.advance(6)
    p.followup_reply.pop('continuation')
    result=await e.handle(follow_event('再讲一点',event_id='second'))
    messages=p.calls[-1]['messages']
    assert any('CUSTOM_CONTINUATION_HEADER' in m['content'] for m in messages)
    assert contract(messages)['required']==['text','mode']
    assert result['mode']=='text' and len(p.plan_calls)==1
    assert 'participation_plan' in contract(p.plan_calls[-1]['messages'])['required']
    trace=e.trajectories.list()['items'][0]
    assert trace['status']=='success' and trace['contract_errors']==[]


def test_idle_without_recent_chat_ends_in_empty_transcript_not_a_fake_member_message():
    from azusa_bot.request_context import render
    from azusa_bot.prompts import DEFAULTS
    messages=[{'role':'system','content':'PERSONA'},{'role':'user','content':DEFAULTS['group_header']+'{}'}]
    rendered=render(messages,kind='group_idle',prefixes=DEFAULTS,contract=build(Settings(group_vote_enabled=True),'group_idle'),contract_text='FORMAT')
    assert rendered[-1]['role']=='user'
    assert json.loads(rendered[-1]['content'].split('\n',1)[1])=={'recent_conversation':[]}
    assert 'current_message' not in str(rendered)
