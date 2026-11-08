"""Examples and bounded tool execution through real context/trace boundaries."""
import json
import pytest
from azusa_bot import conversation_config as config, lore
from azusa_bot.prompts import prompt
from azusa_bot.core import Incoming
from test_web_search import rig, event
from test_app import http


def configure(engine, **changes):
    old=config.describe(engine.content_root)
    config.save(engine.content_root, old['config'] | changes, old['revision'])


class Model:
    def __init__(self, answers): self.answers=iter(answers); self.calls=[]
    async def complete(self, messages, **kw):
        self.calls.append(messages)
        return next(self.answers)


@pytest.mark.asyncio
async def test_examples_are_actual_pairs_before_context_without_becoming_memory(rig):
    engine,_,_,_=rig
    configure(engine, begin_dialogs=['例子里的问题','例子里的回答','第二个例子','第二个回应'])
    model=Model([{'text':'本轮实际回答。','mode':'text'}]);engine.provider_override=model
    reply=await engine.handle(event(text='本轮问题'))
    assert reply['mode']=='text'
    assert [(m['role'],m['content']) for m in model.calls[0][1:5]]==[
        ('user','例子里的问题'),('assistant',json.dumps({'text':'例子里的回答','mode':'text'},ensure_ascii=False)),
        ('user','第二个例子'),('assistant',json.dumps({'text':'第二个回应','mode':'text'},ensure_ascii=False))]
    assert model.calls[0][5]['content'].startswith(prompt(engine.content_root,'direct_header',engine.settings()))
    engine.acknowledge(reply['reply_id'],'sent','900')
    assert '例子里的' not in str(engine.dialogue.get(reply['scope'],'20001',ttl_seconds=300))
    assert engine.memory.stats()['history']==0
    trace=engine.trajectories.list()['items'][0]
    assert trace['begin_dialogs']['pairs']==2 and trace['context_budget']['example_tokens']>0


@pytest.mark.asyncio
async def test_examples_disabled_and_summary_never_receive_them(rig):
    engine,_,_,_=rig;configure(engine, examples_enabled=False, begin_dialogs=['EXAMPLE','RESPONSE'])
    model=Model([{'text':'实际回答','mode':'text'}]);engine.provider_override=model
    await engine.handle(event())
    assert 'EXAMPLE' not in str(model.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('order',['local_first','search_first'])
async def test_loop_combines_tools_and_preserves_both_evidence_sources(rig,order):
    engine,_,_,searches=rig
    lore.import_documents(engine.content_root,[{'text':'星河：星河是合成测试的名字。','category':'reference'}])
    local={'knowledge_query':'星河'};web={'web_search':{'query':'Python 发布说明'}}
    model=Model([local,web,{'text':'两份资料都查到了。','mode':'text'}] if order=='local_first' else [web,local,{'text':'两份资料都查到了。','mode':'text'}])
    engine.provider_override=model
    reply=await engine.handle(event())
    assert reply['mode']=='text' and len(model.calls)==3 and len(searches)==1
    data=json.loads(model.calls[-1][1]['content'].split('\n',1)[1])
    assert data['knowledge_lookup']['status']=='found' and data['web_evidence']['status']=='success'
    trace=engine.trajectories.list()['items'][0]
    assert trace['execution']['model_call']==3 and trace['execution']['tool_rounds']==2
    assert set(trace['execution']['tools'])=={'web_search','knowledge_lookup'}


@pytest.mark.asyncio
async def test_loop_zero_rounds_and_single_call_never_searches(rig):
    engine,_,_,searches=rig;configure(engine,execution={'max_tool_rounds':0,'max_model_calls':1})
    model=Model([{'text':'现在没有查实。','web_search':{'query':'Python 版本'}}]);engine.provider_override=model
    reply=await engine.handle(event())
    assert reply['mode']=='text' and len(model.calls)==1 and not searches
    assert prompt(engine.content_root,'execution_done',engine.settings()) in model.calls[0][0]['content']


def test_admin_validation_revision_profile_isolation_and_preview(http):
    client,root,engine,admin,bridge=http
    assert client.get('/admin/conversation',headers=bridge).status_code==401
    old=client.get('/admin/conversation',headers=admin).json();cfg=old['config']
    cfg['begin_dialogs']=['提问','回应'];cfg['segmented_reply']['enabled']=True
    saved=client.put('/admin/conversation',headers=admin,json={'config':cfg,'revision':old['revision']})
    assert saved.status_code==200
    assert client.put('/admin/conversation',headers=admin,json={'config':cfg,'revision':old['revision']}).status_code==409
    preview=client.post('/admin/conversation/preview',headers=admin,json={'config':cfg,'text':'这一句是完整的开头。下一句是另外一句话！最后还有一个结尾。'}).json()
    assert len(preview['segments'])==3
    second=engine.profiles.create({'name':'乙'})['id'];engine.activate(second)
    assert client.get('/admin/conversation',headers=admin).json()['config']['begin_dialogs']==[]
    engine.activate('default')
    assert config.read(engine.content_root)['begin_dialogs']==['提问','回应']
    cfg['begin_dialogs']=['缺少回答']
    assert client.put('/admin/conversation',headers=admin,json={'config':cfg,'revision':saved.json()['revision']}).status_code==400


@pytest.mark.parametrize('text',[
    '短句。短句！还有一个相对更长的结尾。',
    '第一句话讲到这里。第二句话也讲完了！第三句话结束了。第四句话仍然保留。',
    '链接 https://example.com/a?x=1。不能拆坏。',
    '代码 `hello()`。保持完整。',
    '第一段\n第二段\n第三段\n第四段',
])
def test_segmentation_preserves_content_and_caps_bubbles(text):
    import re
    parts=config.split_text(text,{'enabled':True,'max_segments':3,'interval_seconds':0})
    assert 1<=len(parts)<=3
    assert re.sub(r'\s','',''.join(parts))==re.sub(r'\s','',text)
    if 'http' in text or '`' in text:assert parts==[text]


@pytest.mark.asyncio
async def test_trace_resolves_only_matching_example_version(rig):
    engine,_,_,_=rig;configure(engine,begin_dialogs=['例子问题','例子答案'])
    engine.provider_override=Model([{'text':'本轮回答','mode':'text'}])
    await engine.handle(event())
    trace=engine.trajectories.list()['items'][0]
    configure(engine,segmented_reply={'enabled':True,'max_segments':2,'interval_seconds':0})
    detail=engine.trajectories.get(trace['id'],engine.content_root)
    assert detail['references']['begin_dialogs']['available']
    configure(engine,begin_dialogs=['新的问题','新的答案'])
    detail=engine.trajectories.get(trace['id'],engine.content_root)
    assert not detail['references']['begin_dialogs']['available'] and not detail['references']['begin_dialogs']['messages']


def test_example_budget_can_block_without_dropping_persona_or_examples():
    from test_context_budget import direct
    from azusa_bot.context_budget import fit_context
    messages,refs=direct()
    plain=fit_context(messages,refs,kind='reply',window_tokens=4096)
    with_examples=fit_context(messages,refs,kind='reply',window_tokens=4096,example_tokens=5000)
    assert not plain['blocked'] and with_examples['blocked']
    assert with_examples['messages'][0]==messages[0] and with_examples['metrics']['example_tokens']==5000
