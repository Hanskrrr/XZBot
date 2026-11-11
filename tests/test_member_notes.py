"""Member introductions and dated summaries use synthetic local data only."""
import json
import time
from unittest.mock import AsyncMock

import pytest
from azusa_bot import member_notes
from azusa_bot.member_context import select
from azusa_bot.memory import MemoryStore
from azusa_bot.trajectories import references
from context_helpers import context
from test_group_engine import rig, event, SCOPE
from test_memory_admin import local

USER='20001'


@pytest.fixture
def store():
    store=MemoryStore(':memory:')
    store.set_consent(SCOPE,USER,True)
    yield store
    store.close()


def source(store,text='我叫小明，喜欢猫',age=0,event_id='a',user=USER):
    store.set_consent(SCOPE,user,True)
    number=store.add_history(SCOPE,user,'user',text,event_id)
    with store._db:store._db.execute('UPDATE history SET created_at=? WHERE id=?',(time.time()-age,number))
    return number


def organize(store,number,*,user=USER,intro='小明喜欢猫。',interaction='11月9日谈起养猫的经历。',revisit=False):
    batch=store.begin_summary(SCOPE,user,force=True,revisit=revisit)['batch']
    batch['previous_background']=member_notes.background(store,SCOPE,user)
    return store.finish_summary(SCOPE,user,batch,[],
        background={'text':intro,'source_ids':[number]} if intro else None,
        interactions=[{'text':interaction,'source_ids':[number]}] if interaction else [])


def test_fixed_intro_recent_72h_and_older_retrieval(store):
    old=source(store,'前几天谈过排位连败',age=73*3600,event_id='old')
    organize(store,old,intro=None,interaction='前几天谈过排位连败')
    current=source(store,event_id='new');organize(store,current)
    rows=select(store,SCOPE,USER,'你好')
    assert [r['note_type'] for r in rows]==['background','interaction']
    assert [r['selection_reason'] for r in rows]==['member_background','recent_interaction']
    assert len(member_notes.recent(store,SCOPE,USER))==1
    assert any('排位连败' in r['text'] and r['selection_reason']=='current_message' for r in select(store,SCOPE,USER,'排位连败'))
    # Reorganizing yesterday's database does not reset the occurrence time.
    organize(store,old,intro=None,interaction='前几天谈过排位连败',revisit=True)
    assert len(member_notes.recent(store,SCOPE,USER))==1
    assert len([r for r in store.list_memories(SCOPE,USER) if '排位连败' in r['text']])==1


def test_background_versions_manual_pin_and_provenance_erasure(store):
    first=source(store);organize(store,first)
    intro=member_notes.background(store,SCOPE,USER);assert intro['version']==1
    second=source(store,'也喜欢打游戏',event_id='b');organize(store,second,intro='小明喜欢猫和游戏。')
    intro=member_notes.background(store,SCOPE,USER)
    assert intro['version']==2 and set(intro['source_ids'])=={first,second}
    store.delete_history(first,SCOPE,USER)
    assert member_notes.background(store,SCOPE,USER) is None
    member_notes.edit_background(store,SCOPE,USER,'手动介绍',version=0)
    with pytest.raises(ValueError,match='member_background_changed'):
        member_notes.edit_background(store,SCOPE,USER,'过期编辑',version=0)
    third=source(store,'今天上线了',event_id='c');organize(store,third,intro='不能覆盖手动介绍')
    assert member_notes.background(store,SCOPE,USER)['text']=='手动介绍'
    store.set_consent(SCOPE,USER,False)
    assert member_notes.recent(store,SCOPE,USER)==[]
    assert member_notes.background(store,SCOPE,USER)['source']=='explicit'
    store.forget_me(SCOPE,USER)
    assert member_notes.background(store,SCOPE,USER) is None


@pytest.mark.parametrize('bad',[{'text':'错误引用','source_ids':[999999]}, {'text':'（（隐私内容','source_ids':[1]}])
def test_invalid_note_rolls_back_all_summary_writes(store,bad):
    n=source(store);batch=store.begin_summary(SCOPE,USER,force=True)['batch']
    with pytest.raises(ValueError):
        store.finish_summary(SCOPE,USER,batch,[{'text':'不应部分保存','source_ids':[n]}],interactions=[bad])
    assert store.list_memories(SCOPE,USER)==[]


def test_reply_sources_do_not_advance_past_unprocessed_human_message(store):
    n=source(store,event_id='first');batch=store.begin_summary(SCOPE,USER,force=True,limit=1)['batch']
    pending=source(store,'下一条尚未整理',event_id='next')
    reply=store.add_history(SCOPE,USER,'assistant','那下次一起玩吧','first:reply')
    batch['history']=member_notes.summary_dialogue(store,SCOPE,USER,batch['history'])
    assert [r['id'] for r in batch['history']]==[n,reply]
    store.finish_summary(SCOPE,USER,batch,[],interactions=[{'text':'小明提到兴趣，角色提议下次一起玩。','source_ids':[n,reply]}])
    assert store.begin_summary(SCOPE,USER,force=True)['batch']['history'][0]['id']==pending
    store.delete_history(reply,SCOPE,USER)
    assert member_notes.recent(store,SCOPE,USER)==[]


def test_related_members_load_only_scoped_cards_and_consent(store):
    mine=source(store);organize(store,mine)
    peer=source(store,'我叫小红',user='20002',event_id='peer');organize(store,peer,user='20002',intro='小红。')
    store.add_memory(SCOPE,'20002','没有被明确要求的私人往事')
    store.add_memory('onebot:10001:dm:20002','20002','私聊内容')
    rows=select(store,SCOPE,USER,'你好',related_users=['20002'])
    other=[r for r in rows if r['subject']=='20002']
    assert len(other)==2 and all(r.get('note_type') for r in other)
    assert not any('私人往事' in r['text'] or '私聊内容' in r['text'] for r in rows)
    store.set_consent(SCOPE,'20002',False)
    assert not any(r['subject']=='20002' for r in select(store,SCOPE,USER,'你好',related_users=['20002']))


@pytest.mark.asyncio
async def test_model_receives_fixed_cards_before_current_dialogue_without_extra_call(rig):
    engine,settings,provider,clock=rig
    n=source(engine.memory);organize(engine.memory,n)
    result=await engine.handle(event('你好',unprompted=False,mentioned=True,event_id='test'))
    assert result['mode']=='text' and len(provider.calls)==1
    wire=context(provider.calls[0]['messages'])
    card=wire['member_context'][0]
    assert card['background']['text']=='小明喜欢猫。' and len(card['recent_interactions'])==1
    assert 'member_context' in provider.calls[0]['messages'][1]['content']
    assert 'current_message' in provider.calls[0]['messages'][-1]['content']
    trace=engine.trajectories.list()['items'][0]
    detail=engine.trajectories.get(trace['id'],engine.content_root)
    assert any(r.get('version')==1 and r['available'] for r in detail['references']['memories'])


@pytest.mark.asyncio
async def test_summary_includes_prior_background_and_confirmed_reply_in_same_call(rig):
    engine,settings,provider,clock=rig
    n=source(engine.memory);organize(engine.memory,n)
    second=source(engine.memory,'最近在排位',event_id='second')
    reply=engine.memory.add_history(SCOPE,USER,'assistant','下次一起玩呀','second:reply')
    def answer(messages):
        rows=json.loads(messages[-1]['content'])
        assert rows[0]['previous_member_background']['text']=='小明喜欢猫。'
        assert any(r['id']==reply and r['role']=='assistant' for r in rows)
        return {'memories':[],'background':{'text':'小明喜欢猫，也玩排位。','source_ids':[second]},
                'interactions':[{'text':'小明聊到排位，角色提议一起玩。','source_ids':[second,reply]}]}
    provider.summary_answer=answer
    result=await engine.summarize(SCOPE,USER,provider,settings,force=True)
    assert result['status']=='saved' and len(provider.calls)==1
    assert member_notes.background(engine.memory,SCOPE,USER)['version']==2


def test_admin_editor_requires_auth_and_rejects_stale_revision(local):
    client,engine,admin,_=local
    url='/admin/memory/member-background';body={'scope':SCOPE,'user':USER,'text':'小明喜欢猫。','version':0}
    assert client.put(url,json=body).status_code==401
    assert client.put(url,json=body,headers=admin).status_code==200
    assert client.put(url,json=body,headers=admin).status_code==409
    data=client.get('/admin/memory/member-context',params={'scope':SCOPE,'user':USER},headers=admin).json()
    assert data['background']['source']=='explicit' and data['window_hours']==72
    schedule=data['schedule']
    assert schedule['poll_seconds']==60 and schedule['threshold']==4
    assert schedule['pending']==0 and not schedule['ready'] and not schedule['enabled']


def test_budget_drops_optional_recall_before_fixed_intro_and_current_dialogue(store):
    from test_context_budget import direct,fit,payload
    n=source(store);organize(store,n)
    notes=select(store,SCOPE,USER,'你好')
    extra=dict(notes[0],id=999,text='旧检索结果'*1000,selection_reason='current_message')
    messages,refs=direct(mem=notes+[extra])
    result=fit(messages,refs,budget=4000)
    retained=payload(result['messages'][1])['retrieved_memory']
    assert not result['blocked']
    assert [r['id'] for r in retained]==[r['id'] for r in notes]
    assert result['messages'][-1]==messages[-1]
    assert len(result['refs']['memories'])==len(notes)


def test_related_trajectory_version_and_consent_redaction(store):
    n=source(store,user='20002');organize(store,n,user='20002')
    rows=select(store,SCOPE,USER,'你好',related_users=['20002'])
    refs=references(memories=rows)
    trace=store.trajectories.begin('reply','direct',SCOPE,USER,'mock','mock',refs)
    from pathlib import Path
    detail=store.trajectories.get(trace,Path('.'))
    assert all(r['available'] and r['subject']=='20002' for r in detail['references']['memories'])
    store.set_consent(SCOPE,'20002',False)
    detail=store.trajectories.get(trace,Path('.'))
    assert all(not r['available'] for r in detail['references']['memories'])
    assert '小明' not in json.dumps(detail,ensure_ascii=False)


def test_member_card_api_rejects_profile_change(local):
    client,engine,admin,_=local
    response=client.put('/admin/memory/member-background',headers=admin,
        json={'scope':SCOPE,'user':USER,'text':'不能写进别的角色','version':0,'profile_id':'a'*32})
    assert response.status_code==409
    assert member_notes.background(engine.memory,SCOPE,USER) is None
