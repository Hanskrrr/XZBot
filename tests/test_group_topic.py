"""Topic confidence is gated before generation; no cloud calls or real chat."""
import asyncio
import json
import math
import pytest
from azusa_bot import group_topic, lore
from azusa_bot.core import Ledger
from azusa_bot.providers import ProviderError
from azusa_bot.settings import Settings
from azusa_bot.context_budget import fit_context
from azusa_bot.trajectory_admin import attach_delivery
from test_group_engine import rig, event, configure, SCOPE


class TopicModel:
    def __init__(self):
        self.calls=[];self.score=.91;self.invalid_source=False;self.related=True
        self.failure=False;self.entered=None;self.release=None
    async def complete(self,messages,**kw):
        self.calls.append(messages)
        data=json.loads(messages[1]['content'].split('\n',1)[1])
        anchor=data['current_message']
        if 'text' not in anchor:
            if self.entered:self.entered.set()
            if self.release:await self.release.wait()
            if self.failure:raise ProviderError('test_topic_failure')
            other=next(r for r in data['recent_group_messages'] if r['user_id']!=anchor['user_id'])
            return {'topic':{'summary':'大家在聊英雄联盟排位中的打野配合。','confidence':self.score,
                'relevant_to_current':self.related,'anchor_message_id':anchor['message_id'],
                'source_message_ids':[anchor['message_id'],'nonexistent' if self.invalid_source else other['message_id']]}}
        return {'text':'这波打得还挺热闹。','mode':'text','confidence':.95,'participate':True}


def setup(rig,**changes):
    engine,settings,_,clock=rig
    configure(engine,settings,group_topic_enabled=True,**changes)
    for n in range(2):
        engine.groups.add(SCOPE,str(21000+n),'英雄联盟打野又来帮忙了 '+str(n),str(900+n));clock.advance(1)
    model=TopicModel();engine.provider_override=model
    return engine,settings,model,clock


@pytest.mark.asyncio
async def test_high_topic_enters_actual_reply_and_lore_retrieval_with_separate_trace(rig):
    engine,settings,model,clock=setup(rig)
    doc=lore.import_documents(engine.content_root,[{'title':'测试英雄联盟术语','text':'英雄联盟打野负责野区与支援。','category':'reference','tags':['英雄联盟']}])['documents'][0]
    reply=await engine.handle(event('他这波啥意思',event_id='1001'))
    assert reply['mode']=='text' and len(model.calls)==2
    first=json.loads(model.calls[0][1]['content'].split('\n',1)[1]);last=json.loads(model.calls[1][1]['content'].split('\n',1)[1])
    assert first['lore']==[] and first['latest_speaker_own_memories']==[]
    assert last['group_topic']['confidence']==.91
    assert any(r['doc_id']==doc['id'] for r in last['lore'])
    records=engine.trajectories.list()['items']
    assert {r['kind'] for r in records}=={'group_topic','group_decision'}
    assert all(r['group_topic']['status']=='accepted' for r in records)
    assert records[0]['group_topic']['trace_id']==records[1]['id']
    assert attach_delivery(engine,records)[1]['delivery']['state']=='not_applicable'
    raw=engine.trajectories._db.execute('SELECT refs FROM trajectories').fetchall()
    assert all('_topic_rows' not in r[0] for r in raw)


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['low','invalid_sources','unrelated','failure','nan'])
async def test_low_or_invalid_topic_never_enters_reply_but_normal_reply_continues(rig,case):
    engine,settings,model,clock=setup(rig)
    if case=='low':model.score=.6
    if case=='invalid_sources':model.invalid_source=True
    if case=='unrelated':model.related=False
    if case=='failure':model.failure=True
    if case=='nan':model.score=math.nan
    reply=await engine.handle(event('他这波啥意思',event_id='1001'))
    assert reply['mode']=='text' and len(model.calls)==2
    data=json.loads(model.calls[-1][1]['content'].split('\n',1)[1])
    assert 'group_topic' not in data
    row=engine.trajectories.list()['items'][0]
    assert not row['group_topic']['injected']
    if case=='low':assert row['group_topic']['confidence']==.6


@pytest.mark.asyncio
async def test_time_count_limits_and_no_persisted_history_backfill(rig):
    engine,settings,model,clock=setup(rig,group_topic_minutes=1,group_topic_messages=3)
    engine.memory.set_consent(SCOPE,'20001',True)
    engine.memory.add_history(SCOPE,'20001','user','ARCHIVED_PRIVATE_OLD_TOPIC','old-event')
    clock.advance(61)
    for n in range(5):engine.groups.add(SCOPE,str(21000+n%2),'近期群聊主题 '+str(n),str(950+n));clock.advance(1)
    await engine.handle(event('他这波啥意思',event_id='1001'))
    data=json.loads(model.calls[0][1]['content'].split('\n',1)[1])
    assert len(data['recent_group_messages'])==3
    assert all(r['message_id'] not in ['900','901'] for r in data['recent_group_messages'])
    assert 'ARCHIVED_PRIVATE_OLD_TOPIC' not in json.dumps(model.calls)


@pytest.mark.asyncio
async def test_rate_limited_topic_does_not_reuse_previous_summary(rig):
    engine,settings,model,clock=setup(rig)
    await engine.handle(event('他这波啥意思',event_id='1001'))
    clock.advance(16)
    await engine.handle(event('现在说吃饭了',event_id='1002'))
    assert len(model.calls)==3
    assert 'group_topic' not in json.loads(model.calls[-1][1]['content'].split('\n',1)[1])
    assert engine.trajectories.list()['items'][0]['group_topic']['status']=='rate_limit'
    assert Ledger(engine.ledger.path).reserve_topic(SCOPE,settings)=='rate_limit'


@pytest.mark.asyncio
async def test_topic_privacy_cancel_does_not_proceed_to_reply(rig):
    engine,settings,model,clock=setup(rig)
    model.entered=asyncio.Event();model.release=asyncio.Event()
    task=asyncio.create_task(engine.handle(event('他这波啥意思',event_id='1001')))
    await model.entered.wait();engine.invalidate_groups();model.release.set()
    assert (await task)['mode']=='none' and len(model.calls)==1


@pytest.mark.asyncio
async def test_source_erasure_redacts_topic_text_and_ids_even_from_other_members_trace(rig):
    engine,settings,model,clock=setup(rig)
    await engine.handle(event('他这波啥意思',event_id='1001'))
    engine.memory.forget_me(SCOPE,'21000')
    for row in engine.trajectories.list()['items']:
        assert row['group_topic']['redacted']
        assert 'summary' not in row['group_topic'] and 'sources' not in row['group_topic']
        detail=engine.trajectories.get(row['id'],engine.content_root)
        assert 'summary' not in detail['group_topic']


@pytest.mark.asyncio
async def test_final_context_trimming_drops_topic_when_evidence_is_removed(rig):
    engine,settings,model,clock=setup(rig)
    original=engine._complete_traced_once
    async def wrapped(provider,messages,settings,**kwargs):
        if kwargs['kind']=='group_decision':
            refs=kwargs['refs'];data=json.loads(messages[1]['content'].split('\n',1)[1])
            assert 'group_topic' in data
            # An expensive old row forces pruning, including the cited row.
            data['recent_group_messages'][0]['text']='x'*5000
            messages[1]['content']=messages[1]['content'].split('\n',1)[0]+'\n'+json.dumps(data,ensure_ascii=False)
            fitted=fit_context(messages,refs,kind='group_decision',window_tokens=8000)
            if fitted['metrics']['trimmed']:
                assert 'group_topic' not in json.loads(fitted['messages'][1]['content'].split('\n',1)[1])
                assert fitted['refs']['group_topic']['status']=='context_trimmed'
            else:raise AssertionError('test must exercise trimming')
        return await original(provider,messages,settings,**kwargs)
    engine._complete_traced_once=wrapped
    await engine.handle(event('他这波啥意思',event_id='1001'))


def test_topic_budget_is_persistent_and_leaves_last_cloud_reply_available(rig):
    engine,settings,model,clock=setup(rig,group_topic_daily_limit=1)
    assert engine.ledger.reserve_topic(SCOPE,settings) is None
    clock.advance(61)
    assert Ledger(engine.ledger.path).reserve_topic(SCOPE,settings)=='daily_limit'
    settings.provider_mode='cloud';settings.daily_cloud_requests=1;settings.group_topic_daily_limit=30
    assert engine.ledger.reserve_topic('onebot:10001:group:30002',settings)=='cloud_budget'
    assert engine.ledger.consume(1)


def test_lol_starter_is_retrievable_reference_not_always_loaded(tmp_path):
    from pathlib import Path
    if not (Path(__file__).resolve().parents[1]/'examples/knowledge/lol/import.json').exists():pytest.skip("Optional locally imported corpus is not distributed with source")
    docs=json.loads((Path(__file__).resolve().parents[1]/'examples/knowledge/lol/import.json').read_text())['documents']
    assert len(docs)==7 and all(d['category']=='reference' and d['always_include'] is False for d in docs)
    lore.import_documents(tmp_path,docs)
    for query,wanted in [('红温','红温'),('1557','1557'),('四保一','四保一'),('他的XX我很怕','很怕')]:
        found=lore.for_prompt(tmp_path,query)
        assert any(wanted in row['title'] for row in found)
    assert lore.for_prompt(tmp_path,'今天想吃草莓蛋糕')==[]


def hot_setup(rig, **changes):
    from test_prompt_controls_heat import seed_heat
    engine,settings,_,clock=rig
    configure(engine,settings,group_heat_enabled=True,group_topic_enabled=False,**changes)
    seed_heat(engine,clock)
    model=TopicModel();engine.provider_override=model
    return engine,settings,model,clock


@pytest.mark.asyncio
async def test_heat_forces_fresh_topic_despite_normal_switch_and_interval(rig):
    engine,settings,model,clock=hot_setup(rig)
    # A recent optional topic would ordinarily make the next pass rate-limited.
    assert engine.ledger.reserve_topic(SCOPE,settings) is None
    for n in range(2):
        reply=await engine.handle(event('这个打野怎么说 '+str(n),event_id=str(1001+n)))
        assert reply['mode']=='text'
        clock.advance(21)
    assert len(model.calls)==4
    assert sum('text' not in json.loads(c[1]['content'].split('\n',1)[1])['current_message'] for c in model.calls)==2
    assert all(r['group_topic']['required_for_heat'] for r in engine.trajectories.list()['items'])


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['low','invalid_sources','failure','daily','cloud'])
async def test_heat_without_accepted_topic_skips_reply_model_and_records_why(rig,case):
    engine,settings,model,clock=hot_setup(rig)
    expected='low_confidence'
    if case=='low':model.score=.6
    if case=='invalid_sources':model.invalid_source=True;expected='invalid_sources'
    if case=='failure':model.failure=True;expected='unavailable'
    if case=='daily':
        configure(engine,settings,group_topic_daily_limit=1)
        engine.ledger.reserve_topic(SCOPE,settings);expected='daily_limit'
    if case=='cloud':
        configure(engine,settings,provider_mode='cloud',daily_cloud_requests=1);expected='cloud_budget'
    reply=await engine.handle(event('这个打野怎么说',event_id='1001'))
    assert reply['reason']=='heat_topic_not_ready' and reply['mode']=='none'
    assert len(model.calls)==(0 if case in {'daily','cloud'} else 1)
    row=engine.trajectories.list()['items'][0]
    assert row['status']=='topic_blocked' and row['kind']=='group_decision'
    assert row['group_topic']['status']==expected and not row['group_topic']['injected']
    assert not row['provider_called'] and row['response_text'] is None
    assert not engine._group_failures  # Local abstention must not trigger provider backoff.


@pytest.mark.asyncio
async def test_heat_requirement_does_not_block_explicit_at_reply(rig):
    engine,settings,model,clock=hot_setup(rig)
    model.score=.1
    result=await engine.handle(event('这个打野怎么说',event_id='1001',unprompted=False))
    assert result['mode']=='text' and len(model.calls)==1


@pytest.mark.asyncio
async def test_heat_topic_evidence_trimmed_before_reply_stops_paid_generation(rig,monkeypatch):
    engine,settings,model,clock=hot_setup(rig)
    import azusa_bot.core as core
    original=core.fit_context
    def trim(*a,**kw):
        result=original(*a,**kw)
        if kw.get('kind')=='group_decision':
            result['refs']['group_topic'].update(injected=False,status='context_trimmed')
        return result
    monkeypatch.setattr(core,'fit_context',trim)
    result=await engine.handle(event('这个打野怎么说',event_id='1001'))
    assert result['reason']=='heat_topic_not_ready' and len(model.calls)==1
    assert engine.trajectories.list()['items'][0]['group_topic']['status']=='context_trimmed'
