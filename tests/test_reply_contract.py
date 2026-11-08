"""Regressions for effective group controls at the actual provider boundary."""
import json
import pytest
from azusa_bot.reply_contract import build, control_errors
from azusa_bot.settings import Settings, private_write
from azusa_bot.prompts import describe, save
from azusa_bot import conversation_config, lore
from test_group_engine import rig, event, configure, SCOPE, OTHER
from test_group_followup import rig as followup_rig, start, event as follow_event
from test_participation_vote import votes


def contract(messages):
    return json.loads([m['content'] for m in messages[1:] if m['role']=='system'][-1].rsplit('\n',1)[1])


@pytest.mark.parametrize('kind',['reply','group_decision','followup','group_idle'])
@pytest.mark.parametrize('voting',[True,False])
def test_required_fields_match_route_and_enabled_features(kind,voting):
    value=build(Settings(group_vote_enabled=voting,voice_enabled=True,voice_authorized=True),kind,tools=['web_search'])
    required=value['required']
    assert ('votes' in required)==(voting and kind!='reply')
    assert ('continuation' in required)==(kind=='followup')
    assert ('confidence' in required)==(not voting and kind!='reply')
    assert 'voice' in value['properties']['mode']['enum']
    assert ('web_search' in value['properties'])==(kind!='group_idle')
    assert 'threshold' not in str(value) and 'weight' not in str(value)


@pytest.mark.asyncio
async def test_scope_saved_controls_reach_prompt_and_trace_with_examples_and_long_background(rig):
    e,s,p,clock=rig
    private_write(e.root/'config/groups.json',json.dumps({SCOPE:{'group_vote_enabled':True,'group_vote_weight_understanding':70,'group_heat_enabled':True}}))
    configure(e,s,context_window_tokens=300000)
    lore.import_documents(e.content_root,[{'name':'synthetic-long.md','text':'LONG_BACKGROUND_END 测试背景内容。'*2000,'category':'canon','always_include':True}])
    old=conversation_config.describe(e.content_root)
    conversation_config.save(e.content_root,old['config']|{'begin_dialogs':['晚安','嗯，晚安啦。']},old['revision'])
    save(e.content_root,{'reply_contract':'EDITABLE_REQUIRED_FIELDS'},describe(e.content_root,s)['revision'])
    p.answer['votes']=votes(.95)
    reply=await e.handle(event('合成测试问题'))
    assert reply['mode']=='text'
    messages=p.calls[-1]['messages'];actual=contract(messages)
    assert 'votes' in actual['required'] and 'confidence' not in actual['required']
    assert 'EDITABLE_REQUIRED_FIELDS' in messages[-2]['content']
    assert messages[3]['content'].count('LONG_BACKGROUND_END')==2000
    assert 'current_message' in messages[-1]['content']
    trace=e.trajectories.list()['items'][0]
    assert trace['effective_group_settings']['group_vote_weight_understanding']==70
    assert trace['reply_contract']==actual
    assert not e.settings(OTHER).group_vote_enabled
    assert trace['request_layout']['order'][-2:]==['reply_contract','current_message']


@pytest.mark.asyncio
async def test_missing_votes_report_invalid_output_without_an_extra_call_or_fake_scores(rig):
    e,s,p,_=rig;configure(e,s,group_vote_enabled=True)
    p.answer.update(confidence=.99,participate=True)
    result=await e.handle(event())
    trace=e.trajectories.list()['items'][0]
    assert result['reason']=='voting_invalid' and len(p.calls)==1
    assert trace['status']=='invalid_output' and trace['error_code']=='output_contract_invalid'
    assert set(trace['contract_errors'])=={'votes.'+key for key in votes(0)}
    assert trace['participation_vote']['score'] is None
    assert trace['response_text']==p.answer['text']


@pytest.mark.asyncio
async def test_followup_custom_header_survives_serialization_and_continuation_is_required(followup_rig):
    e,s,p,clock=followup_rig
    save(e.content_root,{'followup_header':'CUSTOM_CONTINUATION_HEADER'},describe(e.content_root,s)['revision'])
    await start(e);clock.advance(6)
    p.followup_reply.pop('continuation')
    result=await e.handle(follow_event('再讲一点',event_id='second'))
    messages=p.calls[-1]['messages']
    assert any('CUSTOM_CONTINUATION_HEADER' in m['content'] for m in messages)
    assert 'continuation' in contract(messages)['required']
    assert result['mode']=='none'
    trace=e.trajectories.list()['items'][0]
    assert trace['status']=='invalid_output' and trace['contract_errors']==['continuation']


def test_idle_without_recent_chat_ends_in_empty_transcript_not_a_fake_member_message():
    from azusa_bot.request_context import render
    from azusa_bot.prompts import DEFAULTS
    messages=[{'role':'system','content':'PERSONA'},{'role':'user','content':DEFAULTS['group_header']+'{}'}]
    rendered=render(messages,kind='group_idle',prefixes=DEFAULTS,contract=build(Settings(group_vote_enabled=True),'group_idle'),contract_text='FORMAT')
    assert rendered[-1]['role']=='user'
    assert json.loads(rendered[-1]['content'].split('\n',1)[1])=={'recent_conversation':[]}
    assert 'current_message' not in str(rendered)
