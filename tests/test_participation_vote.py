"""Synthetic weighted criteria, real engine routing/trace, no network or QQ sends."""
import json
import pytest
from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

from azusa_bot.participation_vote import apply, evaluate, weights, CRITERIA, safe_result
from azusa_bot.settings import Settings, write_settings
from azusa_bot.prompts import describe, save
from azusa_bot.group_policies import create_router, effective
from azusa_bot.group_monitor import snapshot
from azusa_bot.trajectory_admin import attach_delivery
from test_group_engine import rig, event, configure, SCOPE, OTHER
from test_group_followup import rig as followup_rig, start, event as follow_event, IDENTITY
from test_group_idle import rig as idle_rig, ready, SELF


def votes(value):return {key:value for key in CRITERIA}


def test_weighted_average_and_zero_weight_are_explicit():
    w=dict(relevance=25,understanding=30,contribution=25,timing=20)
    r=evaluate(dict(relevance=.7,understanding=.9,contribution=.8,timing=.6),w,.8)
    assert r['score']==pytest.approx(.765) and not r['passed']
    assert sum(b['contribution'] for b in r['ballots'])==pytest.approx(r['score'])
    assert evaluate(votes(.7999999),w,.8)['passed'] is False
    assert evaluate(dict(votes(0),timing=1),dict(relevance=0,understanding=0,contribution=0,timing=5),.8)['passed']
    assert safe_result(r)==r


@pytest.mark.parametrize('bad',[None,True,'0.9',-1,1.01,float('nan'),float('inf'),10**1000,{'text':'unexpected'}])
def test_malformed_score_never_falls_back_to_original_confidence(bad):
    s=Settings(group_vote_enabled=True)
    raw={'votes':dict(votes(.99),timing=bad),'participate':True,'confidence':1}
    out=apply(raw,s,'group_decision')
    assert out['_voting_invalid'] and out['participate'] is False and out['confidence'] is None
    assert out['_participation_vote']['invalid_fields']==['timing']
    assert raw['confidence']==1


def test_missing_votes_and_fake_internal_result_are_not_trusted():
    out=apply({'participate':True,'confidence':1,'_participation_vote':{'passed':True}},Settings(group_vote_enabled=True),'followup')
    assert out['_voting_invalid'] and not out['participate']
    assert out['_participation_vote']['invalid_fields']==list(CRITERIA)
    assert '_participation_vote' not in apply({'_participation_vote':{}},Settings(),'reply')


def test_invalid_weights_rejected():
    with pytest.raises(ValidationError):Settings(**{'group_vote_weight_'+k:0 for k in CRITERIA})
    with pytest.raises(ValidationError):Settings(group_vote_weight_timing=True)
    with pytest.raises(ValidationError):Settings(group_vote_weight_timing=101)


@pytest.mark.asyncio
@pytest.mark.parametrize('score,original,expected',[(.95,False,'text'),(.5,True,'none')])
async def test_votes_decide_output_and_trace_keeps_each_criterion_and_candidate(rig,score,original,expected):
    e,s,p,_=rig;configure(e,s,group_vote_enabled=True)
    p.answer.update(votes=votes(score),participate=original,confidence=1 if original else 0)
    reply=await e.handle(event())
    assert reply['mode']==expected and len(p.calls)==1
    row=e.trajectories.list()['items'][0]
    assert row['participation_vote']['score']==pytest.approx(score)
    assert row['confidence']==pytest.approx(score)
    assert len(row['participation_vote']['ballots'])==4
    assert row['response_text']==p.answer['text']
    attach_delivery(e,[row])
    assert row['delivery']['state']==('unconfirmed' if expected=='text' else 'not_sent')
    assert 'votes' in p.calls[0]['messages'][0]['content']
    if expected=='none':assert reply['reason']=='low_confidence'


@pytest.mark.asyncio
async def test_invalid_votes_are_traceable_and_do_not_send(rig):
    e,s,p,_=rig;configure(e,s,group_vote_enabled=True)
    reply=await e.handle(event())
    assert reply['reason']=='voting_invalid' and len(p.calls)==1
    vote=e.trajectories.list()['items'][0]['participation_vote']
    assert vote['status']=='invalid' and len(vote['invalid_fields'])==4


@pytest.mark.asyncio
async def test_direct_mention_does_not_require_a_ballot(rig):
    e,s,p,_=rig;configure(e,s,group_vote_enabled=True)
    reply=await e.handle(event(mentioned=True))
    assert reply['mode']=='text'
    assert 'participation_vote' not in e.trajectories.list()['items'][0]


@pytest.mark.asyncio
async def test_prompt_is_editable_and_vote_weights_are_per_group(rig):
    e,s,p,clock=rig
    from azusa_bot.settings import private_write
    private_write(e.root/'config/groups.json',json.dumps({SCOPE:{'group_vote_enabled':True,'group_vote_weight_timing':80}}))
    save(e.content_root,{'participation_vote':'CUSTOM_VOTE_POLICY'},describe(e.content_root,s)['revision'])
    p.answer['votes']=votes(.99)
    await e.handle(event())
    assert 'CUSTOM_VOTE_POLICY' in p.calls[0]['messages'][0]['content']
    assert e.trajectories.list()['items'][0]['participation_vote']['ballots'][-1]['weight']==80
    assert e.settings(OTHER).group_vote_enabled is False


@pytest.mark.asyncio
async def test_followup_uses_score_but_cannot_override_recipient_judgment(followup_rig):
    e,s,p,clock=followup_rig;s.group_vote_enabled=True;write_settings(e.root,s)
    await start(e);clock.advance(6)
    p.followup_reply.update(votes=votes(.99),continuation=False)
    result=await e.handle(follow_event('和别人说的话',event_id='not-for-you'))
    assert result['reason']=='followup_not_for_bot'
    assert e.trajectories.list()['items'][0]['participation_vote']['passed']


@pytest.mark.asyncio
async def test_low_vote_preserves_followup_window(followup_rig):
    e,s,p,clock=followup_rig;s.group_vote_enabled=True;write_settings(e.root,s)
    await start(e);clock.advance(6)
    p.followup_reply.update(votes=votes(.4),continuation=True)
    result=await e.handle(follow_event('那这个呢',event_id='low'))
    assert result['reason']=='followup_low_confidence' and IDENTITY in e._group_threads


@pytest.mark.asyncio
async def test_idle_runs_without_new_message_after_deadline_and_records_votes(idle_rig):
    e,s,clock,p=idle_rig;s.group_vote_enabled=True;write_settings(e.root,s)
    p.answer.update(votes=votes(.95),participate=False,confidence=0)
    await ready(idle_rig)
    # No second incoming event: the independent timer invokes claim.
    items=await e.idle.claim(SELF)
    assert len(items)==1 and len(p.calls)==1
    row=e.trajectories.list()['items'][0]
    assert row['kind']=='group_idle' and row['participation_vote']['passed']
    clock.advance(61)
    assert await e.idle.claim(SELF)==[] and len(p.calls)==1


@pytest.mark.asyncio
async def test_idle_invalid_votes_stops_once_without_retry_spam(idle_rig):
    e,s,clock,p=idle_rig;s.group_vote_enabled=True;write_settings(e.root,s)
    await ready(idle_rig)
    assert await e.idle.claim(SELF)==[]
    assert e.trajectories.list()['items'][0]['participation_vote']['status']=='invalid'
    clock.advance(61)
    assert await e.idle.claim(SELF)==[] and len(p.calls)==1


def test_admin_settings_roundtrip_and_reject_empty_weights(rig):
    e,*_=rig;app=FastAPI();app.include_router(create_router(SimpleNamespace(current=e)))
    with TestClient(app) as client:
        row=client.get('/admin/group-settings',params={'scope':SCOPE}).json()
        body={'scope':SCOPE,'revision':row['revision'],'settings':{'group_vote_enabled':True,'group_vote_weight_timing':60}}
        saved=client.put('/admin/group-settings',json=body)
        assert saved.status_code==200 and saved.json()['settings']['group_vote_weight_timing']==60
        body.update(revision=saved.json()['revision'],settings={'group_vote_weight_'+k:0 for k in CRITERIA})
        assert client.put('/admin/group-settings',json=body).status_code==422


@pytest.mark.asyncio
async def test_search_gate_and_final_answer_recompute_votes_without_extra_voters(rig):
    e,s,p,_=rig;configure(e,s,group_vote_enabled=True,web_search_enabled=True)
    searches=[]
    async def search(query):
        searches.append(query)
        return {'status':'no_results','query':query,'results':[]}
    e.search_override=search
    async def complete(messages,**kwargs):
        p.calls.append({'messages':messages})
        if len(p.calls)==1:
            return {'votes':votes(.95),'web_search':{'query':'公开游戏版本信息'},'confidence':0,'participate':False}
        return {'votes':votes(.3),'mode':'text','text':'欸，这个没查到，我先不瞎说了。','confidence':1,'participate':True}
    p.complete=complete
    result=await e.handle(event())
    assert result['mode']=='none' and len(p.calls)==2 and len(searches)==1
    rows=e.trajectories.list()['items']
    ballots=[r['participation_vote'] for r in rows if 'participation_vote' in r]
    assert len(ballots)==2 and not ballots[0]['passed'] and ballots[1]['passed']
    from azusa_bot.prompts import prompt
    assert p.calls[-1]['messages'][0]['content'].count(prompt(e.content_root,'participation_vote',s))==1


@pytest.mark.asyncio
async def test_low_votes_cannot_start_paid_search(rig):
    e,s,p,_=rig;configure(e,s,group_vote_enabled=True,web_search_enabled=True)
    p.answer.update(votes=votes(.2),web_search={'query':'公开游戏版本信息'},confidence=1)
    async def forbidden(_):raise AssertionError('search should not be called')
    e.search_override=forbidden
    result=await e.handle(event())
    assert result['mode']=='none' and len(p.calls)==1
    assert all(r['kind']!='web_search' for r in e.trajectories.list()['items'])
