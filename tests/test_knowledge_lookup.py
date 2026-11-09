"""Bounded profile-local progressive retrieval through the real reply/trace path."""
import json
import pytest
from azusa_bot import lore, knowledge_lookup, role_activity
from azusa_bot.core import Ledger
from azusa_bot.settings import Settings,write_settings
from test_web_search import rig,event,SCOPE
from test_role_activity import row,enable
import time


class LookupModel:
    def __init__(self,query='星河跳舞'):self.query=query;self.calls=[];self.repeat=False
    async def complete(self,messages,**kw):
        self.calls.append(messages)
        data=json.loads(messages[1]['content'].split('\n',1)[1])
        if self.repeat or 'knowledge_lookup' not in data:return {'knowledge_query':self.query}
        return {'text':'一起跳舞呀。','mode':'text'}


@pytest.mark.asyncio
async def test_directory_detail_lookup_links_traces_keeps_activity_and_avoids_web(rig):
    engine,settings,_,web=rig;model=LookupModel();engine.provider_override=model
    doc=lore.import_documents(engine.content_root,[{'text':'星河跳舞：大家转着圈庆祝。','category':'reference'}])['documents'][0]
    r=row(time.time());enable(engine.content_root,[r]);role_activity.sync(engine.content_root,[r])
    answer=await engine.handle(event(text='这个梗啥意思'))
    assert answer['mode']=='text' and len(model.calls)==2 and not web
    data=json.loads(model.calls[-1][1]['content'].split('\n',1)[1])
    assert data['knowledge_lookup']['status']=='found'
    assert doc['id'] in data['knowledge_lookup']['document_ids']
    assert data['role_activity']['recent_window_hours']==24
    assert '自己的近期' in model.calls[-1][0]['content'] or '近期生活与公开活动记录' in model.calls[-1][0]['content']
    assert '默认怀疑' in model.calls[-1][0]['content']
    assert '这个梗啥意思' in model.calls[-1][-1]['content']
    records=engine.trajectories.list()['items'];assert len(records)==2
    assert records[0]['related_id']==records[1]['id'] and records[1]['status']=='research_requested'
    assert records[0]['knowledge_lookup']['query']=='星河跳舞'
    detail=engine.trajectories.get(records[0]['id'],engine.content_root)
    assert any(x['selection_reason']=='recent_activity' for x in detail['references']['knowledge'])
    engine.trajectories.erase_previews(scope=SCOPE)
    assert all(x['knowledge_lookup'] is None and x['role_activity'] is None for x in engine.trajectories.list()['items'])


@pytest.mark.asyncio
async def test_recent_unrelated_post_is_not_a_lookup_hit_or_recursion(rig):
    engine,_,_,web=rig;model=LookupModel('超级外星企鹅');engine.provider_override=model
    r=row(time.time());enable(engine.content_root,[r]);role_activity.sync(engine.content_root,[r])
    await engine.handle(event(text='这个词是什么'))
    assert engine.trajectories.list()['items'][0]['knowledge_lookup']['status']=='no_match'
    model.repeat=True
    await engine.handle(event(text='再来一次',event_id='2'))
    assert len(model.calls)==4 and not web


@pytest.mark.asyncio
async def test_expansion_can_retrieve_late_paragraph_of_recent_post(rig):
    engine,_,_,web=rig;model=LookupModel('紫晶翼龙');engine.provider_override=model
    r=row(time.time());payload=json.loads(r['payload'])
    payload['events'][0]['card']['blocks'][0]['text']='普通日常。'*500+'紫晶翼龙出现在结尾。'
    r['payload']=json.dumps(payload);enable(engine.content_root,[r]);role_activity.sync(engine.content_root,[r])
    await engine.handle(event(text='继续这个话题'))
    first=json.loads(model.calls[0][1]['content'].split('\n',1)[1])
    final=json.loads(model.calls[1][1]['content'].split('\n',1)[1])
    assert all('紫晶翼龙' not in x['text'] for x in first['lore'])
    assert any('紫晶翼龙' in x['text'] and x['start_char']>1200 for x in final['lore'])
    assert final['knowledge_lookup']['status']=='found' and not web


@pytest.mark.asyncio
async def test_lookup_second_round_obeys_same_cloud_budget(rig):
    engine,settings,_,web=rig;settings.provider_mode='cloud';settings.daily_cloud_requests=1
    write_settings(engine.root,settings);model=LookupModel();engine.provider_override=model
    await engine.handle(event(text='这个词是什么'))
    assert len(model.calls)==1 and not web
    assert engine.trajectories.list()['items'][0]['status']=='cloud_budget'


@pytest.mark.parametrize('kind,answer,valid',[
    ('reply',{'knowledge_query':'星河'},True),
    ('group_decision',{'knowledge_query':'星河','participate':True,'confidence':.1},False),
    ('followup',{'knowledge_query':'星河','participate':True,'confidence':.99},False),
    ('followup',{'knowledge_query':'星河','participate':True,'continuation':True,'confidence':.99},True),
    ('reply',{'knowledge_query':{'bad':'query'}},False),
])
def test_query_has_same_participation_gate(kind,answer,valid):
    assert bool(knowledge_lookup.query(answer,kind,Settings()))==valid


def test_budget_does_not_claim_discarded_lookup_details_were_read():
    from test_context_budget import direct,knowledge,DIRECT_PREFIX
    from azusa_bot.context_budget import fit_context
    messages,refs=direct(lore=knowledge()[:1])
    data=json.loads(messages[1]['content'][len(DIRECT_PREFIX):])
    lookup={'status':'found','query':'资料','document_ids':['doc-0'],'parent_trace_id':1}
    data['knowledge_lookup']=lookup;refs['knowledge_lookup']=dict(lookup)
    messages[1]['content']=DIRECT_PREFIX+json.dumps(data,ensure_ascii=False)
    fitted=fit_context(messages,refs,kind='reply',window_tokens=4096,output_tokens=1200)
    assert not fitted['blocked'] and fitted['refs']['knowledge']==[]
    assert fitted['refs']['knowledge_lookup']['status']=='context_trimmed'
    assert fitted['refs']['knowledge_lookup']['retained_document_ids']==[]
