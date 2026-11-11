"""Per-message opt-out across ingress, storage and retrieval; no live services."""
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest

from azusa_bot import history_lookup
from azusa_bot.core import QuotedMessage
from azusa_bot.memory import MemoryStore
from azusa_bot.message_privacy import hidden_message
from azusa_bot.trajectories import fingerprint
from context_helpers import context
from test_group_engine import rig, event, SCOPE


@pytest.mark.parametrize('text,hidden', [
    ('（（这句忽略', True), ('((这句忽略', True), (' \t\n\u3000（（', True),
    ('((/记住 不应执行', True), ('（普通括号）', False), ('(普通括号)', False),
    ('这里提到（（不是前缀', False), ('（(混用不作为标记', False), ('', False),
])
def test_prefix_contract(text, hidden):
    assert hidden_message(text) is hidden


@pytest.mark.asyncio
@pytest.mark.parametrize('updates', [
    {}, {'unprompted':False,'mentioned':True}, {'activity_only':True},
    {'images':['https://not-fetched.invalid/image.jpg']},
    {'kind':'dm','scope_id':'20001','unprompted':False},
    {'transport':'official','unprompted':False},
])
async def test_hidden_message_stops_before_all_side_effects(rig, monkeypatch, updates):
    engine,settings,provider,clock=rig
    engine.memory.set_consent(SCOPE,'20001',True)
    observe=Mock(side_effect=AssertionError('must not observe hidden content'))
    prepare=AsyncMock(side_effect=AssertionError('must not download hidden attachments'))
    monkeypatch.setattr(engine.idle,'observe',observe)
    monkeypatch.setattr(engine.vision,'prepare',prepare)
    before=engine.ledger.deliveries()
    for demo in (False,True):
        reply=await engine.handle(event('  （（请记住这句私密内容',**updates),demo=demo)
        assert reply=={'mode':'none','text':'','reason':'message_hidden'}
    assert not provider.calls
    assert engine.ledger.deliveries()==before
    assert engine.memory.scope_stats(SCOPE)=={'history':0,'memories':0}
    assert engine.groups.snapshot(SCOPE)==[]
    assert engine.groups.familiarity(SCOPE,'20001') is None
    assert engine.trajectories.list()['items']==[]
    observe.assert_not_called();prepare.assert_not_called()


@pytest.mark.asyncio
async def test_hidden_message_does_not_change_consent_or_block_next_message(rig):
    engine,settings,provider,clock=rig
    engine.memory.set_consent(SCOPE,'20001',True)
    await engine.handle(event('((/记忆 关闭',unprompted=False,mentioned=True))
    result=await engine.handle(event('下一句正常聊天',event_id='2',unprompted=False,mentioned=True))
    assert result['mode']=='text'
    assert len(provider.calls)==1
    rows=engine.memory.recent_history(SCOPE,'20001')
    assert [r['text'] for r in rows]==['下一句正常聊天']
    assert '记忆 关闭' not in json.dumps(provider.calls,ensure_ascii=False)


@pytest.mark.asyncio
async def test_normal_reply_cannot_expose_hidden_quote_to_model_or_trace(rig):
    engine,settings,provider,clock=rig
    quote=QuotedMessage(message_id='99',user_id='20002',kind='group',scope_id='30001',
                        text='（（引用中不该出现的私密内容',sent_at=int(time.time()),media_types=['image'])
    result=await engine.handle(event('这句话是什么意思',unprompted=False,mentioned=True,
                                    reply_to_id='99',quoted_message=quote))
    assert result['mode']=='text'
    wire=context(provider.calls[-1]['messages'])
    assert '引用中不该出现' not in json.dumps(provider.calls,ensure_ascii=False)
    assert 'quoted_message' not in wire['current_message']
    assert wire['current_message']['quote_status']=='unavailable'
    for row in engine.trajectories.list()['items']:
        detail=engine.trajectories.get(row['id'],engine.content_root)
        assert '引用中不该出现' not in json.dumps(detail,ensure_ascii=False)


def test_storage_and_legacy_history_recall_respect_opt_out():
    store=MemoryStore(':memory:')
    try:
        store.set_consent(SCOPE,'20001',True)
        assert store.add_history(SCOPE,'20001','user','（（不能写入','new') is None
        # Synthetic old-version database, not a real member's data.
        with store._db:
            old=store._db.execute('INSERT INTO history(scope,user_id,role,text,event_id,created_at) VALUES (?,?,?,?,?,?)',
                (SCOPE,'20001','user','((旧版本保留的秘密','old',time.time())).lastrowid
        assert store.begin_summary(SCOPE,'20001',force=True)['status']=='empty'
        valid_id=store.add_history(SCOPE,'20001','user','喜欢打游戏','visible')
        batch=store.begin_summary(SCOPE,'20001',force=True)['batch']
        assert [r['id'] for r in batch['history']]==[valid_id]
        store.finish_summary(SCOPE,'20001',batch,[])
        revisit=store.begin_summary(SCOPE,'20001',force=True,revisit=True)['batch']
        assert [r['id'] for r in revisit['history']]==[valid_id]
        assert [r['id'] for r in store.recent_history(SCOPE,'20001')]==[valid_id]
        assert [r['id'] for r in store.summary_candidates(SCOPE,'20001')]==[valid_id]
        assert history_lookup.select(store,SCOPE,'20001','秘密')[0]==[]
        lookup={'references':[{'id':old,'sha256':fingerprint('((旧版本保留的秘密')}]}
        assert not history_lookup.valid(store,SCOPE,'20001',{'history_lookup':lookup})
        assert history_lookup.resolve(store,SCOPE,'20001',lookup)==[{'id':old,'available':False}]
    finally:
        store.close()


def test_group_context_rejects_hidden_message_even_if_called_directly(rig):
    engine,*_=rig
    assert engine.groups.add(SCOPE,'20001','（（私密内容','1') is None
    assert engine.groups.familiarity(SCOPE,'20001') is None
    assert engine.groups.snapshot(SCOPE)==[]
