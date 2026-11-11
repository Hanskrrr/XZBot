"""A malformed plan must not suppress a later explicit @ in another group."""
import pytest

from azusa_bot.providers import ProviderError
from test_group_engine import rig, event, configure
from test_memory_admin import local


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['conversation_plan_invalid', 'deepseek_output_not_json',
                                   'deepseek_output_not_object', 'gemini_balance_output_not_json'])
async def test_bad_plan_does_not_silence_other_groups(rig, failure):
    engine, settings, provider, _ = rig
    configure(engine, settings, provider_mode='cloud')

    def invalid(_):
        if failure == 'conversation_plan_invalid':
            return {}
        raise ProviderError(failure)

    provider.plan_answer = invalid
    first = await engine.handle(event('今天大家想聊什么？'))
    # The existing JSON-contract repair allows one retry for this specific code.
    expected_calls = 2 if failure == 'deepseek_output_not_json' else 1
    assert first['mode'] == 'none' and len(provider.plan_calls) == expected_calls
    assert engine._cloud_failure_count == 0 and engine._cloud_retry_after == 0
    assert engine.diagnostics.status()['services'][0]['code'] == failure
    provider.plan_answer = None
    direct = await engine.handle(event('在吗？', event_id='2', scope_id='30002',
        unprompted=False, mentioned_user_ids=['10001']))
    assert direct['mode'] == 'text' and len(provider.calls) == 1
    with engine.ledger.db() as db:
        row = db.execute('SELECT * FROM deliveries WHERE reply_id=?',(direct['reply_id'],)).fetchone()
    assert row['trigger_kind'] == 'mention' and row['status'] == 'prepared'


@pytest.mark.asyncio
async def test_actual_service_outage_keeps_backoff_and_records_why_mention_was_skipped(rig):
    engine, settings, provider, _ = rig
    configure(engine, settings, provider_mode='cloud')
    engine.provider_failure(ProviderError('deepseek_http_503'), settings)
    deadline = engine._cloud_retry_after
    engine.provider_failure(ProviderError('conversation_plan_invalid'), settings)
    assert engine._cloud_retry_after == deadline and engine._cloud_failure_count == 1
    result = await engine.handle(event('', mentioned_user_ids=['10001'], unprompted=False))
    assert result['reason'] == 'cloud_backoff' and not provider.all_calls
    with engine.ledger.db() as db:
        row = db.execute('SELECT * FROM deliveries WHERE reply_id=?',(result['reply_id'],)).fetchone()
    assert row['trigger_kind'] == 'mention' and row['error_code'] == 'deepseek_http_503'
    assert row['retry_after_seconds'] == 60 and row['queue_wait_seconds'] < 1
    assert engine.trajectories.list()['items'] == []


def test_event_api_exposes_trigger_without_saving_message_body(local):
    import asyncio
    from azusa_bot.settings import write_settings
    client, manager, admin, _ = local
    engine = manager.current
    settings = engine.settings().model_copy(update={'onebot_enabled':True,'group_chat_enabled':True,'provider_mode':'cloud'})
    write_settings(engine.root, settings)
    engine.provider_failure(ProviderError('deepseek_http_503'), settings)
    asyncio.run(engine.handle(event('仅用于测试的秘密正文', mentioned_user_ids=['10001'], unprompted=False)))
    data = client.get('/admin/trajectories/events', headers=admin).json()
    row = data['items'][0]
    assert row['trigger_kind'] == 'mention' and not row['model_called']
    assert row['error_code'] == 'deepseek_http_503' and row['retry_after_seconds'] > 0
    assert '秘密正文' not in str(data)
