"""Explicit local history reads stay dated, scoped, consented and traceable."""
import json,time
import pytest
from azusa_bot import history_lookup
from azusa_bot.memory import MemoryStore
from azusa_bot.context_budget import fit_context
from test_web_search import rig,event
from test_conversation_execution import Model


def test_no_automatic_cross_scope_member_or_unmatched_fallback():
    memory=MemoryStore(':memory:');scope='onebot:10001:dm:20001';user='20001'
    for s,u in [(scope,user),(scope,'20002'),('onebot:10001:group:30003',user)]:
        memory.set_consent(s,u,True);memory.add_history(s,u,'user','周末计划去游泳','1')
    rows,status=history_lookup.select(memory,scope,user,'周末计划')
    assert status=='found' and len(rows)==1 and rows[0]['scope']==scope and rows[0]['user_id']==user
    assert history_lookup.select(memory,scope,user,'不存在的词')[1]=='no_match'
    memory.set_consent(scope,user,False)
    assert history_lookup.select(memory,scope,user,'最近')==([],'recording_disabled')
    memory.close()


@pytest.mark.asyncio
async def test_on_demand_lookup_is_in_next_request_and_trace_and_correctable(rig):
    engine,_,_,_=rig;e=event(text='我之前说周末要做什么？');scope='onebot:10001:dm:20001'
    # event() in this fixture is a group message; use its actual tenant key.
    scope=f'{e.transport}:{e.app_id}:{e.kind}:{e.scope_id}'
    engine.memory.set_consent(scope,e.user_id,True)
    old=engine.memory.add_history(scope,e.user_id,'user','周末计划去游泳','998')
    model=Model([{'history_query':'周末计划'},{'text':'你之前说要去游泳。','mode':'text'}]);engine.provider_override=model
    answer=await engine.handle(e)
    assert answer['mode']=='text' and len(model.calls)==2
    context=json.loads(model.calls[-1][1]['content'].split('\n',1)[1])
    assert context['requested_history'][0]['id']==old and context['requested_history'][0]['time_utc']
    assert not context.get('images')
    trace=engine.trajectories.list()['items'][0]
    assert trace['history_lookup']['status']=='found' and trace['counts']['history_lookup']==1
    detail=engine.trajectories.get(trace['id'],engine.content_root)
    assert detail['references']['requested_history'][0]['available']
    engine.memory.update_history(old,scope,e.user_id,'周末改为看书')
    detail=engine.trajectories.get(trace['id'],engine.content_root)
    assert not detail['references']['requested_history']


@pytest.mark.asyncio
async def test_disabled_recording_and_tool_budget_never_read_arbitrary_history(rig):
    engine,_,_,_=rig;model=Model([{'history_query':'最近'},{'text':'提醒我一下刚才说到哪了？','mode':'text'}]);engine.provider_override=model
    result=await engine.handle(event())
    assert result['mode']=='text'
    data=json.loads(model.calls[-1][1]['content'].split('\n',1)[1]);assert data['history_lookup']['status']=='recording_disabled'
    assert data['requested_history']==[]
    assert history_lookup.query({'history_query':'最近'},'group_decision',engine.settings()) is None


def test_history_lookup_removed_before_current_context_when_budget_full():
    from test_context_budget import direct
    messages,refs=direct();data=json.loads(messages[1]['content'].split('\n',1)[1])
    data['requested_history']=[{'id':1,'text':'旧记录'*4000}];data['history_lookup']={'status':'found'}
    messages[1]['content']=messages[1]['content'].split('\n',1)[0]+'\n'+json.dumps(data,ensure_ascii=False)
    refs['history_lookup']={'status':'found','references':[{'id':1,'sha256':'x'}]}
    fit=fit_context(messages,refs,kind='reply',window_tokens=4096)
    assert not fit['blocked'] and fit['metrics']['removed']['history_lookup']==1
    assert fit['refs']['history_lookup']['status']=='context_trimmed'
