"""Synthetic dispatch, quote provenance and local visibility; no live services."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest
from azusa_bot.core import Engine,QuotedMessage
from azusa_bot.quote_origins import resolve
from context_helpers import context
from test_group_engine import rig,event,SCOPE
from test_memory_admin import local


def quoted(reply_id='90001',text='回答',user='20002',message='2'):
    quote=QuotedMessage(message_id=reply_id,user_id='10001',kind='group',scope_id='30001',text=text,sent_at=int(time.time()))
    return event('这句是在说什么？',user_id=user,event_id=message,mentioned=True,unprompted=False,reply_to_id=reply_id,quoted_message=quote)


@pytest.mark.asyncio
async def test_other_speaker_does_not_cancel_voted_answer_but_expiry_does(rig):
    engine,settings,provider,clock=rig
    provider.entered,provider.release=asyncio.Event(),asyncio.Event()
    waiting=asyncio.create_task(engine.handle(event('这个怎么玩？')))
    await asyncio.wait_for(provider.entered.wait(),2)
    engine.groups.add(SCOPE,'20002','我也来看看','extra')
    provider.release.set();result=await waiting
    assert result['mode']=='text'
    engine.acknowledge(result['reply_id'],'sent','90001')
    clock.advance(60)
    provider.entered,provider.release=asyncio.Event(),asyncio.Event()
    waiting=asyncio.create_task(engine.handle(event('新问题？',event_id='3')))
    await asyncio.wait_for(provider.entered.wait(),2)
    clock.advance(46);provider.release.set();result=await waiting
    assert result['mode']=='none' and result['reason']=='context_changed_during_generation'


@pytest.mark.asyncio
async def test_quote_by_another_member_loads_original_question_after_restart(rig):
    engine,settings,provider,clock=rig
    engine.memory.set_consent(SCOPE,'20001',True)
    first=await engine.handle(event('昨天约好周六打游戏，对吗？',mentioned=True,unprompted=False))
    engine.acknowledge(first['reply_id'],'sent','90001')
    fresh=Engine(engine.root)
    try:
        origin=resolve(fresh,quoted(text=first['text']))
        assert origin['status']=='resolved' and origin['method']=='receipt_link'
        assert origin['original_questions'][0]['text']=='昨天约好周六打游戏，对吗？'
        assert origin['original_questions'][0]['user_id']=='20001'
        fresh.provider_override=provider
        result=await fresh.handle(quoted(text=first['text']))
        assert result['mode']=='text'
        payload=context(provider.calls[-1]['messages'])['current_message']
        assert payload['user_id']=='20002'
        assert payload['quote_origin']['original_questions'][0]['user_id']=='20001'
        trace=fresh.trajectories.list()['items'][0]
        detail=fresh.trajectories.get(trace['id'],fresh.content_root)
        assert detail['quote_origin']['original_questions'][0]['available']
    finally:
        fresh.groups.close();fresh.memory.close()


@pytest.mark.asyncio
async def test_quote_origin_chain_and_scope_privacy(rig):
    engine,settings,provider,clock=rig
    engine.memory.set_consent(SCOPE,'20001',True)
    first=await engine.handle(event('周六几点集合？',mentioned=True,unprompted=False))
    engine.acknowledge(first['reply_id'],'sent','90001')
    second=await engine.handle(quoted(text=first['text'],user='20001'))
    engine.acknowledge(second['reply_id'],'sent','90002')
    origin=resolve(engine,quoted('90002',second['text'],message='3'))
    assert [r['message_id'] for r in origin['original_questions']]==['1','2']
    wrong=quoted('90002',second['text']).model_copy(update={'scope_id':'30002'})
    assert resolve(engine,wrong)['status']=='unavailable'
    engine.memory.set_consent(SCOPE,'20001',False)
    assert resolve(engine,quoted('90002',second['text']))['status']=='unavailable'


@pytest.mark.asyncio
async def test_only_confirmed_sends_are_linked_and_deleted_sources_not_revived(rig):
    engine,settings,provider,clock=rig
    engine.memory.set_consent(SCOPE,'20001',True)
    first=await engine.handle(event('不能丢失原问题',mentioned=True,unprompted=False))
    engine.acknowledge(first['reply_id'],'uncertain')
    assert resolve(engine,quoted(text=first['text']))['status']=='unavailable'
    second=await engine.handle(event('新的原问题',event_id='2',mentioned=True,unprompted=False))
    engine.acknowledge(second['reply_id'],'sent','90002')
    source=engine.memory.recent_history(SCOPE,'20001')[-1]
    engine.memory.delete_history(source['id'],SCOPE,'20001');engine.clear_group_context(SCOPE)
    assert resolve(engine,quoted('90002',second['text']))['status']=='unavailable'


@pytest.mark.asyncio
async def test_legacy_quote_requires_unique_exact_confirmed_answer(rig):
    engine,settings,provider,clock=rig
    engine.memory.set_consent(SCOPE,'20001',True)
    first=await engine.handle(event('旧版原问题',mentioned=True,unprompted=False))
    engine.acknowledge(first['reply_id'],'sent','90001')
    with engine.memory._db:engine.memory._db.execute('DELETE FROM quote_origins')
    engine.dialogue.clear()
    origin=resolve(engine,quoted(text=first['text']))
    assert origin['method']=='verified_legacy_answer'
    assert resolve(engine,quoted(text='相似但不是同一段文字'))['status']=='unavailable'
    second=await engine.handle(event('另一个问题',event_id='2',mentioned=True,unprompted=False))
    engine.acknowledge(second['reply_id'],'sent','90002')
    with engine.memory._db:engine.memory._db.execute('DELETE FROM quote_origins')
    engine.dialogue.clear()
    assert resolve(engine,quoted(text=first['text']))['status']=='unavailable'


def test_local_events_are_authenticated_and_ignore_private_prefix(local):
    client,manager,admin,_=local
    assert client.get('/admin/trajectories/events').status_code==401
    engine=manager.current
    from azusa_bot.settings import write_settings
    settings=engine.settings().model_copy(update={'onebot_enabled':True,'group_chat_enabled':True,'group_chat_min_messages':20})
    write_settings(engine.root,settings)
    asyncio.run(engine.handle(event('普通的聊天',event_id='11')))
    asyncio.run(engine.handle(event('（（不要留下内容或处理记录',event_id='12')))
    rows=client.get('/admin/trajectories/events',headers=admin,params={'scope':SCOPE}).json()['items']
    assert len(rows)==1 and rows[0]['source_event_id']=='11'
    assert rows[0]['status']=='collecting_context' and not rows[0]['model_called']
    assert '普通的聊天' not in json.dumps(rows,ensure_ascii=False)


def test_planning_only_event_links_actual_model_inputs_without_copying_chat(local):
    from azusa_bot.providers import mock_group_plan
    from azusa_bot.settings import write_settings
    from test_group_engine import Provider

    client, manager, admin, _ = local
    engine = manager.current
    write_settings(engine.root, engine.settings().model_copy(update={
        'onebot_enabled': True, 'group_chat_enabled': True, 'group_chat_min_messages': 2,
        'group_chat_debounce_seconds': 0,
    }))
    provider = engine.provider_override = Provider()

    def observe(data):
        answer = mock_group_plan(data)
        answer['participation_plan'].update(action='observe', target='unknown')
        return answer

    provider.plan_answer = observe
    first = asyncio.run(engine.handle(event('不应复制的较早消息', event_id='101')))
    second = asyncio.run(engine.handle(event('新消息触发规划', event_id='opaque-102')))
    assert first['reason'] == 'collecting_context'
    assert second['reason'] == 'conversation_observed' and not provider.calls
    events = client.get('/admin/trajectories/events', headers=admin).json()['items']
    observed = next(row for row in events if row['reply_id'] == second['reply_id'])
    assert observed['window_entered'] and observed['model_call_count'] == 1
    assert observed['request_tail'] == '新消息触发规划'
    assert observed['request_preview_state'] == 'saved' and not observed['peer_bot']
    assert observed['planning']['action'] == 'observe'
    assert observed['planning']['score'] == .95
    path = '/admin/trajectories/events/' + second['reply_id']
    assert client.get(path).status_code == 401
    detail = client.get(path, headers=admin).json()
    assert detail['model_call_count'] == 1 and detail['event']['decision_path'] == 'group_observation'
    assert detail['event']['decision_reason'] == 'message_threshold_met'
    assert detail['stop'] == {'stage': 'planning', 'reason': 'conversation_observed', 'error_code': ''}
    call, = detail['calls']
    assert call['id'] in observed['trajectory_ids'] and call['provider_called']
    assert call['purpose'] == 'participation_plan'
    assert call['response_preview_state'] == 'planning_only'
    inputs = call['input_messages']['items']
    assert [(row['source'], row['message_id']) for row in inputs] == [
        ('recent_conversation', '101'), ('current_message', observed['source_event_id']),
    ]
    assert '不应复制的较早消息' not in json.dumps(detail, ensure_ascii=False)
    skipped = client.get('/admin/trajectories/events/' + first['reply_id'], headers=admin).json()
    assert skipped['event']['window_entered'] and skipped['model_call_count'] == 0
    assert skipped['stop']['reason'] == 'collecting_context'
    assert skipped['event']['request_tail'] is None
    assert skipped['event']['request_preview_state'] == 'unavailable'

    engine.trajectories.erase_previews(scope=SCOPE)
    redacted = client.get(path, headers=admin).json()
    assert redacted['event']['request_tail'] is None and redacted['event']['planning']['action'] is None
    assert redacted['event']['request_preview_state'] == 'redacted'
    assert redacted['model_call_count'] == 1


def test_message_detail_orders_planning_topic_generation_search_and_synthesis(local):
    from azusa_bot.settings import write_settings
    from test_group_topic import TopicModel
    from test_group_voice import Provider as VoiceProvider
    from test_web_search import evidence

    client, manager, admin, _ = local
    engine = manager.current
    write_settings(engine.root, engine.settings().model_copy(update={
        'onebot_enabled': True, 'group_chat_enabled': True, 'group_chat_min_messages': 3,
        'group_chat_debounce_seconds': 0, 'group_topic_enabled': True, 'web_search_enabled': True,
    }))
    manager.profiles.update(engine.profile_id, {'voice': {
        'voice_enabled': True, 'voice_authorized': True, 'fish_reference_id': 'synthetic-voice',
    }})
    audio = VoiceProvider(engine.root)

    class Model(TopicModel):
        async def complete(self, messages, **kwargs):
            result = await super().complete(messages, **kwargs)
            if self.stages[-1] != 'generation':
                return result
            if 'web_evidence' not in context(messages):
                return {'web_search': {'query': '游戏版本公开说明'}}
            return {'text': '看过公开的版本说明了。', 'mode': 'voice'}

        async def synthesize(self, text):
            return await audio.synthesize(text)

    async def search(query):
        return evidence(query)

    model = engine.provider_override = Model()
    engine.search_override = search
    first = asyncio.run(engine.handle(event('群友先聊了版本号', event_id='101', user_id='20002')))
    second = asyncio.run(engine.handle(event('另一个群友也聊了版本', event_id='102', user_id='20003')))
    incoming = event('这次都更新了什么', event_id='103')
    result = asyncio.run(engine.handle(incoming))
    assert first['reason'] == second['reason'] == 'collecting_context'
    assert result['mode'] == 'voice' and len(audio.speech) == 1
    assert model.stages == ['plan', 'topic', 'generation', 'generation']

    detail = client.get('/admin/trajectories/events/' + result['reply_id'], headers=admin).json()
    trigger = detail['event']
    assert (trigger['source_event_id'], trigger['user_id'], trigger['scope']) == ('103', incoming.user_id, SCOPE)
    assert trigger['request_tail'] == incoming.text and trigger['request_preview_state'] == 'saved'
    assert detail['model_call_count'] == 4
    assert [(row['purpose'], row['provider_called']) for row in detail['calls']] == [
        ('participation_plan', True), ('topic_analysis', True), ('group_reply', True),
        ('web_search', True), ('web_search_reply', True), ('speech_synthesis', True),
    ]
    plan, topic, initial, searched, final, synthesized = detail['calls']
    assert initial['related_id'] == plan['id'] and initial['status'] == 'research_requested'
    assert searched['related_id'] == initial['id'] and final['related_id'] == searched['id']
    assert synthesized['related_id'] == final['id']
    assert [item['message_id'] for item in topic['input_messages']['items']] == ['101', '102', '103']
    assert '群友先聊了版本号' not in json.dumps(detail, ensure_ascii=False)
    assert 'SYNTHETIC_PAGE_CONTENT' not in json.dumps(detail)


def test_message_detail_labels_context_retry_as_a_separate_model_call(local):
    from azusa_bot.settings import write_settings
    from test_context_engine import Provider, SCOPE as DM, event as direct_event, seed

    client, manager, admin, _ = local
    engine = manager.current
    write_settings(engine.root, engine.settings().model_copy(update={
        'onebot_enabled': True, 'context_window_tokens': 32768,
    }))
    engine.memory.set_consent(DM, '20001', True)
    seed(engine)
    provider = engine.provider_override = Provider()
    provider.errors = ['context_length_exceeded']
    incoming = direct_event().model_copy(update={'event_id': '301'})
    result = asyncio.run(engine.handle(incoming))
    assert result['mode'] == 'text' and len(provider.calls) == 2
    detail = client.get('/admin/trajectories/events/' + result['reply_id'], headers=admin).json()
    assert detail['event']['source_event_id'] == '301' and detail['event']['request_tail'] == incoming.text
    assert detail['model_call_count'] == 2
    first, retry = detail['calls']
    assert (first['purpose'], first['status'], first['error_code']) == (
        'generate_reply', 'failed', 'context_length_exceeded',
    )
    assert (retry['purpose'], retry['status'], retry['related_id']) == ('context_retry', 'success', first['id'])


def test_legacy_child_with_its_own_numeric_source_is_not_attached_to_parent_message(local):
    from azusa_bot.trajectories import references

    client, manager, admin, _ = local
    engine = manager.current
    first = engine.ledger.reserve(event(event_id='201'), SCOPE, engine.profile_id)
    second = engine.ledger.reserve(event(event_id='202'), SCOPE, engine.profile_id)
    parent = engine.trajectories.begin('reply', 'direct', SCOPE, '20001', 'mock', 'mock',
                                      references(source_event_id='201'))
    tool = engine.trajectories.begin('web_search', 'direct', SCOPE, '20001', 'mock', 'mock',
                                    references(source_event_id='202'), related_id=parent)
    engine.trajectories.called(parent)
    engine.trajectories.called(tool)
    first_detail = client.get('/admin/trajectories/events/' + first, headers=admin).json()
    second_detail = client.get('/admin/trajectories/events/' + second, headers=admin).json()
    assert [row['id'] for row in first_detail['calls']] == [parent]
    assert [row['id'] for row in second_detail['calls']] == [tool]


def test_message_event_pages_keep_order_for_tied_arrival_times(local):
    client, manager, admin, _ = local
    engine = manager.current
    path = '/admin/trajectories/events'
    ids = [engine.ledger.reserve(event(event_id=str(number)), SCOPE, engine.profile_id)
           for number in range(1, 5)]
    other = engine.ledger.reserve(event(event_id='5'), SCOPE, 'another_profile')
    with engine.ledger.db() as db:
        db.execute('UPDATE deliveries SET at=?', (time.time(),))

    first = client.get(path, headers=admin, params={'scope': SCOPE, 'limit': 2}).json()
    newest = engine.ledger.reserve(event(event_id='6'), SCOPE, engine.profile_id)
    with engine.ledger.db() as db:
        db.execute('UPDATE deliveries SET at=? WHERE reply_id=?', (time.time() + 1, newest))
    second = client.get(path, headers=admin, params={
        'scope': SCOPE, 'limit': 2, 'before': first['next_before'],
    }).json()

    assert [item['reply_id'] for page in (first, second) for item in page['items']] == sorted(ids, reverse=True)
    assert second['next_before'] is None
    assert client.get(path, headers=admin, params={'before': 'nan:' + other}).status_code == 400


def test_message_id_search_finds_exact_signed_source_and_sent_receipts(local):
    from azusa_bot.settings import write_settings
    from test_group_engine import OTHER, Provider

    client, manager, admin, _ = local
    engine = manager.current
    write_settings(engine.root, engine.settings().model_copy(update={
        'onebot_enabled': True, 'group_chat_enabled': True, 'group_chat_min_messages': 20,
        'group_chat_debounce_seconds': 0,
    }))
    engine.provider_override = Provider()
    skipped = asyncio.run(engine.handle(event('未触发调用的合成消息', event_id='-101')))
    asyncio.run(engine.handle(event('相近编号的合成消息', event_id='-1010')))
    unsigned = asyncio.run(engine.handle(event('正数编号的合成消息', event_id='101')))
    opaque = asyncio.run(engine.handle(event('非数字编号的合成消息', event_id='synthetic-event')))
    engine.ledger.reserve(event(event_id='-101', scope_id='30002'), OTHER, 'another_profile')
    replied = asyncio.run(engine.handle(event('需要回复的合成消息', event_id='-201', mentioned=True)))
    assert replied['mode'] == 'text'
    engine.acknowledge(replied['reply_id'], 'sent', '-801')
    with engine.ledger.db() as db:
        db.executemany('INSERT INTO delivery_segments VALUES (?, ?, ?, ?)', [
            (replied['reply_id'], 0, 'sent', '-802'),
            (replied['reply_id'], 1, 'uncertain', '-803'),
        ])

    path = '/admin/trajectories/events'
    def matches(message_id, **filters):
        result = client.get(path, headers=admin, params={'message_id':message_id, **filters})
        assert result.status_code == 200
        return result.json()['items']

    row, = matches('-101')
    assert row['reply_id'] == skipped['reply_id'] and row['status'] == 'collecting_context'
    assert row['model_call_count'] == 0
    assert matches('101')[0]['reply_id'] == unsigned['reply_id']
    assert matches('-10') == [] and matches('-101', scope=OTHER) == [] and matches('-101', user='20002') == []
    receipt, = matches('-801', scope=SCOPE, user='20001')
    assert receipt['reply_id'] == replied['reply_id'] and receipt['source_event_id'] == '-201'
    assert receipt['receipt_message_id'] == '-801' and receipt['status'] == 'sent'
    assert matches('-802')[0]['reply_id'] == replied['reply_id'] and matches('-803') == []
    digest = hashlib.sha256(b'synthetic-event').hexdigest()
    assert matches('sha256:' + digest)[0]['reply_id'] == opaque['reply_id']
    invalid = client.get(path, headers=admin, params={'message_id':'synthetic-event'})
    assert invalid.status_code == 400 and invalid.json()['detail'] == 'message_id_filter_invalid'


def test_call_id_search_intersects_filters_and_respects_the_active_profile(local):
    from azusa_bot.trajectories import references
    from test_group_engine import OTHER

    client, manager, admin, _ = local
    store = manager.current.trajectories
    call = store.begin('reply', 'direct', SCOPE, '20001', 'mock', 'mock', references())
    store.begin('summary', 'manual', SCOPE, '20001', 'mock', 'mock', references())
    path = '/admin/trajectories'

    def search(**filters):
        result = client.get(path, headers=admin, params={'call_id':call, **filters})
        assert result.status_code == 200
        return result.json()

    result = search(scope=SCOPE, user='20001', kind='reply', limit=1)
    assert [row['id'] for row in result['items']] == [call] and result['next_before'] is None
    for filters in ({'scope':OTHER}, {'user':'20002'}, {'kind':'summary'}, {'before':call}):
        assert search(**filters)['items'] == []
    assert client.get(path, headers=admin, params={'call_id':0}).status_code == 422
    role = manager.profiles.create({'name':'另一角色'})['id']
    assert client.post(f'/admin/profiles/{role}/activate', headers=admin, json={}).status_code == 200
    result = search()
    assert result['profile_id'] == role and result['items'] == []
    assert manager.current.trajectories.begin('summary', 'manual', SCOPE, '20002', 'mock', 'mock', references()) == call
    found, = search()['items']
    assert found['kind'] == 'summary' and found['user'] == '20002'


def test_cancelled_ack_has_safe_reason_and_never_marks_sent(local):
    client,manager,admin,_=local
    engine=manager.current
    number=engine.ledger.reserve(event(),SCOPE,engine.profile_id,engine.profile_epoch)
    engine.ledger.mark(number,'prepared','text')
    bridge={'Authorization':'Bearer '+(engine.root/'data/bridge.token').read_text().strip()}
    response=client.post('/bridge/ack',headers=bridge,json={'reply_id':number,'status':'cancelled','error_code':'send_authorization_denied'})
    assert response.status_code==200
    rows=client.get('/admin/trajectories/events',headers=admin).json()['items']
    assert rows[0]['status']=='cancelled' and rows[0]['error_code']=='send_authorization_denied'
    assert '没有向 QQ 发出' in rows[0]['explanation']
