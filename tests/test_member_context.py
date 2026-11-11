"""Synthetic member recognition, topic recall and provenance; no paid calls."""
import json
import time

import pytest

from azusa_bot.core import Engine, Incoming
from azusa_bot.memory import MemoryStore
from azusa_bot.providers import mock_group_plan
from azusa_bot.member_context import select
from azusa_bot.settings import Settings, write_settings
from azusa_bot.trajectories import references
from context_helpers import context as wire_context
from test_memory_admin import local

SCOPE='onebot:10001:group:30001'
USER='20001'


@pytest.fixture
def store():
    s=MemoryStore(':memory:')
    yield s
    s.close()


def past(s):
    target=s.add_memory(SCOPE,USER,'曾提到正在练盲僧，希望改善回旋踢。')
    for text in ('喜欢音乐','通常晚上上线','喜欢猫'):
        s.add_memory(SCOPE,USER,text)
    return target


def test_greeting_carries_small_own_background_without_replaying_old_history(store):
    store.set_consent(SCOPE,USER,True)
    store.add_history(SCOPE,USER,'user','旧图片里的神秘物体','old')
    mine=store.add_memory(SCOPE,USER,'群友自称小明，喜欢玩盲僧。')
    for scope,user in [(SCOPE,'20002'),('onebot:10001:group:30002',USER),('onebot:10001:dm:20001',USER)]:
        store.add_memory(scope,user,'不该进来的私密事实')
    rows=select(store,SCOPE,USER,'我又来了')
    assert [r['id'] for r in rows]==[mine]
    assert rows[0]['selection_reason']=='member_background'
    assert not rows[0]['matches_current_message']
    assert '旧图片' not in json.dumps(rows,ensure_ascii=False)
    # The search API itself keeps the previous strict lexical behavior.
    assert store.retrieve(SCOPE,USER,'我又来了')==[]


def test_recent_topic_finds_saved_experience_for_an_elliptical_followup(store):
    target=past(store)
    recent=[dict(role='user',user_id=USER,text='今天练盲僧吗',age_seconds=30)]
    rows=select(store,SCOPE,USER,'后来呢',recent=recent)
    assert next(r for r in rows if r['id']==target)['selection_reason']=='recent_topic'
    for age in (181,None,'30'):
        assert target not in {r['id'] for r in select(store,SCOPE,USER,'后来呢',recent=[dict(recent[0],age_seconds=age)])}
    # A complete new question does not inherit a previous unrelated topic.
    assert target not in {r['id'] for r in select(store,SCOPE,USER,'请介绍一下今天发布的全新版本有哪些功能变化',recent=recent)}


def test_current_query_deduplicates_anchor_and_keeps_shared_facts_query_scoped(store):
    own=store.add_memory(SCOPE,USER,'喜欢猫')
    shared=store.add_memory(SCOPE,'owner','猫咖活动在周六举行',kind='event',is_owner=True)
    rows=select(store,SCOPE,USER,'猫')
    assert len(rows)==2
    assert next(r for r in rows if r['id']==own)['matches_current_message']
    assert next(r for r in rows if r['id']==shared)['selection_reason']=='current_message'
    assert shared not in {r['id'] for r in select(store,SCOPE,USER,'你好')}


def test_no_facts_means_no_invented_profile_and_size_is_bounded(store):
    assert select(store,SCOPE,USER,'你认识我吗')==[]
    store.add_memory(SCOPE,USER,'猫'*7999)
    for i in range(12):store.add_memory(SCOPE,USER,'猫'+str(i)+' 的故事。'*50)
    rows=select(store,SCOPE,USER,'猫',max_chars=800)
    assert len(rows)<=8 and sum(len(r['text']) for r in rows)<=800
    assert all(r['text']==next(m['text'] for m in store.list_memories(SCOPE,USER) if m['id']==r['id']) for r in rows)


def test_source_correction_deletion_and_consent_withdrawal_remove_cached_impressions(store):
    store.set_consent(SCOPE,USER,True)
    source=store.add_history(SCOPE,USER,'user','我喜欢猫','a')
    batch=store.begin_summary(SCOPE,USER,force=True)['batch']
    store.finish_summary(SCOPE,USER,batch,[dict(text='喜欢猫',facet='preference',source_ids=[source])])
    row=select(store,SCOPE,USER,'你好')[0]
    assert row['facet']=='preference'
    store.set_consent(SCOPE,USER,False)
    assert select(store,SCOPE,USER,'你好')==[]
    assert store._db.execute('select count(*) from memory_facets').fetchone()[0]==0
    explicit=store.add_memory(SCOPE,USER,'喜欢茶')
    store.update_memory(explicit,SCOPE,USER,'现在不喝茶')
    assert select(store,SCOPE,USER,'你好')[0]['text']=='现在不喝茶'
    store.delete_memory(explicit,SCOPE,USER)
    assert select(store,SCOPE,USER,'你好')==[]


def test_revisit_empty_extraction_preserves_facts_checks_lease_and_only_uses_retained_history(store):
    store.set_consent(SCOPE,USER,True)
    source=store.add_history(SCOPE,USER,'user','曾经打赢过一场比赛','a')
    batch=store.begin_summary(SCOPE,USER,force=True)['batch']
    assert store.begin_summary(SCOPE,USER,force=True,revisit=True)['status']=='retry_wait'
    store.finish_summary(SCOPE,USER,batch,[])
    explicit=store.add_memory(SCOPE,USER,'明确记住的称呼')
    assert store.begin_summary(SCOPE,USER,force=True)['status']=='empty'
    batch2=store.begin_summary(SCOPE,USER,force=True,revisit=True)['batch']
    assert [r['id'] for r in batch2['history']]==[source]
    result=store.finish_summary(SCOPE,USER,batch2,[dict(text='曾提到赢得一场比赛',facet='experience',source_ids=[source])])
    assert result['saved']==1 and explicit in {r['id'] for r in store.list_memories(SCOPE,USER)}
    store.forget_me(SCOPE,USER)
    assert store.begin_summary(SCOPE,USER,force=True,revisit=True)['status']=='disabled'


def test_six_facts_with_typed_facets_keep_legacy_summaries_compatible(store):
    store.set_consent(SCOPE,USER,True)
    source=store.add_history(SCOPE,USER,'user','合成的自我介绍和经历','a')
    batch=store.begin_summary(SCOPE,USER,force=True)['batch']
    items=[dict(text='合成事实'+str(i),facet=f,source_ids=[source]) for i,f in enumerate(['identity','preference','experience','ongoing','bad',None])]
    assert store.finish_summary(SCOPE,USER,batch,items)['saved']==6
    assert [r['facet'] for r in store.list_memories(SCOPE,USER)]==['identity','preference','experience','ongoing','general','general']


@pytest.fixture
def rig(tmp_path):
    (tmp_path/'content').mkdir();(tmp_path/'content/persona.md').write_text('合成测试角色。')
    write_settings(tmp_path,Settings(transport='onebot',onebot_enabled=True,onebot_self_id='10001',
        onebot_allowed_groups=['30001'],group_chat_enabled=True,group_chat_min_messages=1,
        group_chat_debounce_seconds=0,min_interval_seconds=0,group_followup_min_interval_seconds=2))
    engine=Engine(tmp_path)
    class Provider:
        def __init__(self):self.calls=[];self.plan_calls=[];self.text="晚上好，今天过得怎么样？"
        async def complete(self,messages,**kwargs):
            data=wire_context(messages)
            if data.get('task')=='group_plan':
                self.plan_calls.append(messages)
                return mock_group_plan(data)
            self.calls.append(messages)
            return dict(text=self.text,mode='text',participate=True,continuation=True,confidence=.99)
    provider=Provider();engine.provider_override=provider
    yield engine,provider
    engine.memory.close();engine.groups.close()


def event(text='我又来了',**kw):
    return Incoming(**(dict(transport='onebot',app_id='10001',kind='group',scope_id='30001',user_id=USER,
                           event_id='new',text=text,display_name='小明',mentioned=True)|kw))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['direct','group','followup'])
async def test_all_three_reply_paths_carry_background_before_current_and_trace_why(rig,mode):
    engine,p=rig
    number=engine.memory.add_memory(SCOPE,USER,'这位群友自称小明，喜欢猫。')
    if mode=='followup':
        first=await engine.handle(event('你好',event_id='first'))
        engine.acknowledge(first['reply_id'],'sent','90001')
        engine._group_threads[(SCOPE,USER)]['last_attempt']=time.monotonic()-10
        p.calls.clear();p.text='记得你喜欢猫，它今天还在趴窗边吗？'
    reply=await engine.handle(event(unprompted=mode!='direct',mentioned=mode=='direct'))
    assert reply['mode']=='text' and len(p.calls)==1, reply
    context=wire_context(p.calls[0]);key='retrieved_memory' if mode=='direct' else 'latest_speaker_own_memories'
    assert context[key][0]['id']==number
    assert context[key][0]['selection_reason']=='member_background'
    assert wire_context([p.calls[0][0],p.calls[0][-1]])['current_message']['text']=='我又来了'
    assert '旧原文' not in json.dumps(p.calls,ensure_ascii=False)
    record=engine.trajectories.list()['items'][0]
    detail=engine.trajectories.get(record['id'],engine.content_root)
    memory=detail['references']['memories'][0]
    assert memory['available'] and memory['selection_reason']=='member_background'
    assert memory['subject']==USER


@pytest.mark.asyncio
async def test_no_extra_cloud_request_for_recognition_and_old_raw_text_is_absent(rig):
    engine,p=rig
    engine.memory.set_consent(SCOPE,USER,True)
    engine.memory.add_history(SCOPE,USER,'user','以前发的照片绝不是今天的配图','old')
    engine.memory.add_memory(SCOPE,USER,'以前提到玩过塞尔达')
    result=await engine.handle(event())
    assert result['mode']=='text' and len(p.calls)==1
    assert '以前发的照片' not in json.dumps(p.calls,ensure_ascii=False)


def test_trace_keeps_reason_and_honors_erasure(rig):
    engine,_=rig
    n=engine.memory.add_memory(SCOPE,USER,'喜欢猫')
    rows=select(engine.memory,SCOPE,USER,'猫')
    refs=references(memories=rows)
    assert refs['memories'][0]['selection_reason']=='member_background'
    # A generated trace resolves only currently valid original records.
    t=engine.trajectories.begin(kind='reply',trigger='direct',scope=SCOPE,user=USER,provider='test',model='test',refs=refs)
    engine.memory.delete_memory(n,SCOPE,USER)
    assert not engine.trajectories.get(t,engine.content_root)['references']['memories'][0]['available']


def test_revisit_admin_control_is_strict_and_reprocesses_old_empty_batches(local):
    client,engine,admin,provider=local
    member=dict(scope='onebot:10001:dm:20001',user=USER)
    engine.memory.set_consent(member['scope'],USER,True)
    engine.memory.add_history(member['scope'],USER,'user','曾经赢过合成比赛','a')
    batch=engine.memory.begin_summary(member['scope'],USER,force=True)['batch']
    engine.memory.finish_summary(member['scope'],USER,batch,[])
    url='/admin/memory/summarize'
    assert client.post(url,headers=admin,json=member).json()['status']=='empty'
    assert client.post(url,json=member|dict(revisit=True)).status_code==401
    assert client.post(url,headers=admin,json=member|dict(revisit='true')).status_code==422
    result=client.post(url,headers=admin,json=member|dict(revisit=True)).json()
    assert result['status']=='saved' and provider.calls==1
    assert engine.trajectories.list()['items'][0]['trigger']=='manual_revisit'


def test_memory_roles_survive_context_budget_without_reference_mismatch(rig):
    from azusa_bot.context_budget import fit_context
    engine,_=rig
    for i in range(8):engine.memory.add_memory(SCOPE,USER,'猫'+str(i)+'合成往事'*40)
    evt=event('猫');settings=engine.settings();trace={}
    recent=[dict(role='user',user_id=USER,text='猫',age_seconds=0,message_id='new')]
    messages=engine._group_prompt(evt,SCOPE,settings,recent,trace=trace)
    result=fit_context(messages,trace,kind='group_decision',window_tokens=4096)
    payload=json.loads(result['messages'][1]['content'].split('\n',1)[1])
    rows=payload['latest_speaker_own_memories']
    assert [r['id'] for r in rows]==[r['id'] for r in result['refs']['memories']]
    assert [r['selection_reason'] for r in rows]==[r['selection_reason'] for r in result['refs']['memories']]
