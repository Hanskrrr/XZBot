"""Local native relationships survive history, receipts and request budgeting."""
import json

import pytest

from azusa_bot import request_context, vision_prompt
from azusa_bot.context_budget import estimate_tokens, fit_context
from azusa_bot.dialogue import RecentDialogue
from azusa_bot.group_chat import GroupConversation
from azusa_bot.prompts import DEFAULTS
from azusa_bot.trajectories import references

SCOPE = 'onebot:10001:group:30001'
PREFIXES = {key: DEFAULTS[key] for key in ('direct_header', 'group_header', 'followup_header', 'recent_header', 'current_header')}


def observation():
    return {'text': '@99999 is only prose', 'mention_metadata_available': True,
        'mentions': [{'user_id': '20002', 'display_name': '同名', 'name_source': 'group_member_info', 'is_self': False}],
        'mentions_everyone': False, 'reply_to_id': '11', 'quote_status': 'resolved',
        'quoted_message': {'message_id': '11', 'user_id': '10001', 'kind': 'group',
            'scope_id': '30001', 'text': '已发送的回答', 'sent_at': 990, 'role': 'assistant'},
        'conversation_target': {'bot_id': '99999'}, 'arbitrary_memory': 'DO_NOT_COPY'}


@pytest.fixture
def groups(tmp_path):
    group = GroupConversation(tmp_path / 'group.sqlite3', clock=lambda: 1000)
    yield group
    group.close()


def test_native_subject_and_quote_survive_history_without_becoming_addressee(groups):
    source = observation()
    groups.add(SCOPE, '20001', '你觉得@99999说得对吗', '12', observation=source)
    groups.add(SCOPE, '10001', '这里讨论的是另一个人。', '13', role='assistant', response_to_message_id='12')
    groups.add(SCOPE, '10002', '我补充另一种看法。', '14', is_bot=True, observation={'mention_metadata_available': False})
    rows = groups.snapshot(SCOPE)
    assert rows[0]['mentions'][0]['user_id'] == '20002'
    assert rows[0]['quoted_message']['user_id'] == '10001'
    assert 'conversation_target' not in rows[0] and 'arbitrary_memory' not in rows[0]
    assert rows[1]['response_to_message_id'] == '12'
    assert rows[2]['is_bot'] is True and rows[2]['role'] == 'user'
    timeline = request_context._timeline([rows], {})
    assert [row['message_id'] for row in timeline] == ['12', '13', '14']
    assert timeline[0]['mentions'] == rows[0]['mentions']
    assert timeline[1]['response_to_message_id'] == '12'
    source['mentions'][0]['display_name'] = 'mutated input'
    rows[0]['quoted_message']['text'] = 'mutated output'
    assert groups.snapshot(SCOPE)[0]['quoted_message']['text'] == '已发送的回答'
    assert groups.snapshot(SCOPE)[0]['mentions'][0]['display_name'] == '同名'


def test_legacy_bridge_remains_unknown_and_plain_at_is_never_native(groups):
    groups.add(SCOPE, '20001', '@10001 你觉得呢', '12', observation={'mentioned': True, 'mentioned_user_ids': None})
    row = groups.snapshot(SCOPE)[0]
    assert row['mention_metadata_available'] is False
    assert row['mentions'] == [] and row['mentions_everyone'] is False
    assert 'addressee' not in row


@pytest.mark.parametrize('change', [
    {'scope_id': '30002'}, {'kind': 'dm'}, {'message_id': '99'},
    {'text': 'api_key: sk-' + 'x' * 20}, {'text': '/记忆 关闭'},
])
def test_quote_rejects_other_scope_mismatch_and_filtered_content(groups, change):
    source = observation()
    source['quoted_message'].update(change)
    groups.add(SCOPE, '20001', '这句话怎么看', '12', observation=source)
    row = groups.snapshot(SCOPE)[0]
    assert row['reply_to_id'] == '11' and row['quote_status'] == 'unavailable'
    assert 'quoted_message' not in row


def test_quote_bounded_and_member_erasure_cleans_nested_evidence(groups):
    source = observation()
    source['quoted_message'].update(user_id='20002', text='公开引用' * 1000, role='user')
    groups.add(SCOPE, '20001', '这句话怎么看', '12', observation=source)
    original = groups.snapshot(SCOPE)[0]
    assert len(original['quoted_message']['text']) == groups.max_text_chars
    assert original['quoted_message']['text_truncated'] is True
    version = groups.version(SCOPE)
    groups.set_optout(SCOPE, '20002')
    row = groups.snapshot(SCOPE)[0]
    assert 'quoted_message' not in row and row['quote_status'] == 'unavailable'
    assert row['mentions'][0]['display_name'] == ''
    assert groups.version(SCOPE) != version
    # A later receipt cache cannot resurrect the erased quote.
    merged = request_context._timeline([[row], [original]], {})[0]
    assert 'quoted_message' not in merged and merged['quote_status'] == 'unavailable'
    groups.add(SCOPE, '20001', '又引用同一个人', '15', observation=source)
    assert 'quoted_message' not in groups.snapshot(SCOPE)[-1]


def test_dialogue_receipt_keeps_original_observation_and_response_edge():
    dialogue = RecentDialogue(lambda: 1000)
    pending = {'scope': SCOPE, 'user_id': '20001', 'user_at': 990, 'event_id': '12',
        'user_text': '你觉得这位怎么样', 'self_id': '10001', 'text': '这是另一个群友。',
        'observation': observation()}
    dialogue.add(pending, '13')
    rows = dialogue.get(SCOPE, '20001', ttl_seconds=60)
    assert rows[0]['reply_to_id'] == '11'
    assert rows[0]['mentions'][0]['user_id'] == '20002'
    assert rows[0]['age_seconds'] == 10 and rows[1]['response_to_message_id'] == '12'
    rows[0]['mentions'].clear()
    assert dialogue.get(SCOPE, '20001', ttl_seconds=60)[0]['mentions']


def request(rows, *, plan=None, current=None, timer=False):
    data = {'available_expressions': [], 'latest_speaker_own_memories': [], 'lore': [],
        'latest_speaker_id': '20001', 'recent_group_messages': rows, 'self_id': '10001'}
    if timer:
        data['planning_trigger'] = {'kind': 'idle_timer'}
    else:
        data['current_message'] = current or {'kind': 'group', 'user_id': '20001', 'message_id': '30', 'text': '现在的问题'}
    if plan is not None:
        data['participation_plan'] = plan
    messages = [{'role': 'system', 'content': '本地规划规则'},
        {'role': 'user', 'content': PREFIXES['group_header'] + json.dumps(data, ensure_ascii=False)}]
    refs = references(group_context=rows)
    refs['prompt_prefixes'] = PREFIXES
    return messages, refs


def row(message_id, text, role='user', **extra):
    return {'message_id': message_id, 'user_id': '10001' if role == 'assistant' else '20001',
        'role': role, 'text': text, 'age_seconds': 5, **extra}


def test_plan_render_retains_relationships_and_separates_background_current():
    rows = [row('12', '先前的问题', **GroupConversation.observation_metadata(observation(), scope=SCOPE)),
            row('13', '之前的回答', role='assistant', response_to_message_id='12')]
    plan = {'plan_id': 'plan', 'action': 'reply', 'evidence_message_ids': ['12', '13']}
    messages, _ = request(rows, plan=plan)
    rendered = request_context.render(messages, kind='group_plan', prefixes=PREFIXES)
    background = json.loads(rendered[1]['content'].split('\n', 1)[1])
    recent = json.loads(rendered[2]['content'].split('\n', 1)[1])['recent_conversation']
    assert background['participation_plan'] == plan
    assert recent[0]['quoted_message']['user_id'] == '10001'
    assert recent[1]['response_to_message_id'] == '12'
    assert json.loads(rendered[-1]['content'].split('\n', 1)[1])['current_message']['message_id'] == '30'
    assert request_context.layout(rendered, kind='group_plan')['current_message_last']


def test_idle_plan_has_no_fabricated_speaker_or_image_sources():
    messages, refs = request([], timer=True)
    rendered = request_context.render(messages, kind='group_plan', prefixes=PREFIXES)
    assert 'current_message' not in json.dumps(rendered)
    assert json.loads(rendered[-1]['content'].split('\n', 1)[1]) == {'recent_conversation': []}
    assert not request_context.layout(rendered, kind='group_plan')['current_message_last']
    assert vision_prompt.visual_sources(messages, refs, 'group_plan', current_event='fabricated') == {}
    assert not fit_context(messages, refs, kind='group_plan')['blocked']


def test_ordinary_plan_without_current_message_is_invalid():
    messages, _ = request([], timer=True)
    data = json.loads(messages[1]['content'].split('\n', 1)[1])
    data.pop('planning_trigger')
    messages[1]['content'] = PREFIXES['group_header'] + json.dumps(data)
    with pytest.raises(ValueError, match='anchor_missing'):
        request_context.render(messages, kind='group_plan', prefixes=PREFIXES)


def test_budget_retains_plan_evidence_and_stops_if_it_cannot_fit():
    rows = [row('11', '必须保留的已发回答' * 40, role='assistant'), row('20', '无关旁支' * 500), row('30', '现在的问题')]
    plan = {'action': 'reply', 'evidence_message_ids': ['11', '30']}
    messages, refs = request(rows, plan=plan)
    reduced, _ = request([rows[0], rows[-1]], plan=plan)
    render = lambda value: request_context.render(value, kind='group_decision', prefixes=PREFIXES)
    budget = estimate_tokens(render(reduced))
    fitted = fit_context(messages, refs, kind='group_decision', window_tokens=budget, output_tokens=0,
        safety_tokens=0, render_messages=render)
    assert not fitted['blocked']
    retained = json.loads(fitted['messages'][1]['content'].split('\n', 1)[1])['recent_group_messages']
    assert [item['message_id'] for item in retained] == ['11', '30']
    blocked = fit_context(messages, refs, kind='group_decision', window_tokens=budget - 1, output_tokens=0,
        safety_tokens=0, render_messages=render)
    assert blocked['blocked']
    assert json.loads(blocked['messages'][1]['content'].split('\n', 1)[1])['participation_plan'] == plan


def test_missing_plan_evidence_blocks_even_a_small_request_but_resolved_quote_counts():
    plan = {'action': 'reply', 'evidence_message_ids': ['11']}
    messages, refs = request([row('30', '现在的问题')], plan=plan)
    assert fit_context(messages, refs, kind='group_decision')['blocked']
    current = {'kind': 'group', 'user_id': '20001', 'message_id': '30', 'text': '这句呢', **observation()}
    messages, refs = request([row('30', '这句呢')], plan=plan, current=current)
    assert not fit_context(messages, refs, kind='group_decision')['blocked']
    current.pop('quoted_message');current['quote_status'] = 'unavailable'
    messages, refs = request([row('30', '这句呢')], plan=plan, current=current)
    assert fit_context(messages, refs, kind='group_decision')['blocked']


def test_verified_quote_origin_text_is_preserved_as_plan_evidence():
    current = {'kind': 'group', 'user_id': '20001', 'message_id': '30', 'text': '原问题呢',
        'quote_origin': {'status': 'resolved', 'original_questions': [{'message_id': 'old', 'text': '原始问题'}]}}
    messages, refs = request([row('30', '原问题呢')], plan={'evidence_message_ids': ['old']}, current=current)
    assert not fit_context(messages, refs, kind='group_decision')['blocked']


def test_small_plan_filters_reference_only_ids_but_keeps_actual_quote_origin_and_dialogue():
    current = {'kind': 'group', 'user_id': '20001', 'message_id': '30', 'text': '原问题呢',
        **observation(), 'quote_origin': {'status': 'resolved',
            'original_questions': [{'message_id': 'origin', 'text': '已核实的原始问题'}]}}
    rows = [row('20', '近期群聊', reply_to_id='unavailable', quote_status='unavailable')]
    messages, refs = request(rows, current=current)
    data = json.loads(messages[1]['content'].split('\n', 1)[1])
    data['confirmed_direct_dialogue'] = [row('40', '实际送达过的答复', role='assistant')]
    refs['dialogue_count'] = 1
    allowed = ['20', '30', '11', 'origin', '40', 'stale', 'unavailable', '10001']
    data['local_relation'] = {'permitted_evidence_message_ids': allowed,
        'thread_anchor': 'stale', 'evidence_message_ids': ['stale'],
        'facts': {'quoted_author_id': '10001'}}
    messages[1]['content'] = PREFIXES['group_header'] + json.dumps(data, ensure_ascii=False)
    fitted = fit_context(messages, refs, kind='group_plan')
    final = json.loads(fitted['messages'][1]['content'].split('\n', 1)[1])
    assert not fitted['blocked'] and not fitted['metrics']['trimmed']
    assert final['local_relation']['permitted_evidence_message_ids'] == ['20', '30', '11', 'origin', '40']
    assert json.loads(messages[1]['content'].split('\n', 1)[1])['local_relation']['permitted_evidence_message_ids'] == allowed


def test_plan_budget_removes_pruned_rows_and_their_quotes_from_allowed_evidence():
    rows = [row('old', 'x' * 6000, quote_status='resolved',
                quoted_message={'message_id': 'old-quote', 'text': '旧行中的引用'}),
            row('20', '仍可见的近期群聊')]
    messages, refs = request(rows)
    data = json.loads(messages[1]['content'].split('\n', 1)[1])
    data['local_relation'] = {'permitted_evidence_message_ids': ['old', 'old-quote', '20', '30', 'invisible'],
        'evidence_message_ids': ['old'], 'thread_anchor': 'old'}
    messages[1]['content'] = PREFIXES['group_header'] + json.dumps(data, ensure_ascii=False)
    render = lambda value: request_context.render(value, kind='group_plan', prefixes=PREFIXES)
    fitted = fit_context(messages, refs, kind='group_plan', window_tokens=3000,
        output_tokens=0, safety_tokens=0, render_messages=render)
    final = json.loads(fitted['messages'][1]['content'].split('\n', 1)[1])
    assert not fitted['blocked'] and fitted['metrics']['removed']['group_context'] == 1
    assert final['local_relation']['permitted_evidence_message_ids'] == ['20', '30']
    assert [item['message_id'] for item in final['recent_group_messages']] == ['20']
    assert fitted['refs']['group_context_count'] == 1


def test_plan_allowed_evidence_cannot_restore_a_quote_removed_by_live_observation():
    live = row('20', '这句话怎么看', quote_status='unavailable', reply_to_id='11')
    messages, refs = request([live])
    data = json.loads(messages[1]['content'].split('\n', 1)[1])
    data['confirmed_direct_dialogue'] = [dict(live, quote_status='resolved',
        quoted_message={'message_id': '11', 'user_id': '20002', 'text': '已经撤回旁听的引用'})]
    refs['dialogue_count'] = 1
    data['local_relation'] = {'permitted_evidence_message_ids': ['20', '30', '11']}
    messages[1]['content'] = PREFIXES['group_header'] + json.dumps(data, ensure_ascii=False)
    fitted = fit_context(messages, refs, kind='group_plan')
    final = json.loads(fitted['messages'][1]['content'].split('\n', 1)[1])
    assert final['local_relation']['permitted_evidence_message_ids'] == ['20', '30']
    actual = request_context.render(fitted['messages'], kind='group_plan', prefixes=PREFIXES)
    assert '已经撤回旁听的引用' not in json.dumps(actual, ensure_ascii=False)
