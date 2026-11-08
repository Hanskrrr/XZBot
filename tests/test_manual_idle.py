"""Explicit admin conversations bypass automatic timing, never delivery permissions."""
import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from test_group_idle import rig, activity, SELF, GROUP, SCOPE
from azusa_bot.settings import write_settings, token
from azusa_bot.app import create_app
from azusa_bot.trajectory_admin import attach_delivery

async def test_manual_without_activity_or_auto_settings_does_not_request_votes(rig):
    e,s,clock,p=rig
    s.group_chat_enabled=False;s.group_idle_enabled=False;s.group_idle_groups=[];s.group_vote_enabled=True
    s.group_idle_start_hour=s.group_idle_end_hour=1
    write_settings(e.root,s)
    p.answer={'text':'最近想找个游戏玩。','mode':'text','participate':False,'confidence':0}
    number='a'*32
    assert e.idle.queue_manual(SCOPE,number)['status']=='queued'
    assert e.idle.queue_manual(SCOPE,number)['id']==number
    assert not p.calls
    item,=await e.idle.claim_manual(SELF)
    assert len(p.calls)==1 and item['manual'] is True
    assert p.calls[0][-1]['role']=='user'
    assert await e.idle.claim_manual(SELF)==[]
    assert e.idle.authorize(item['id'],item['claim_token'],SELF)
    assert not e.idle.authorize(item['id'],item['claim_token'],SELF)
    assert e.idle.ack(item['id'],item['claim_token'],SELF,'sent','123')['acknowledged']
    assert e.idle.manual_status(number)['status']=='sent'
    assert e.groups.snapshot(SCOPE)[-1]['role']=='assistant'
    trace=e.trajectories.list()['items'][0]
    assert trace['kind']=='group_idle_manual'
    assert trace['reply_contract']['required']==['text','mode']
    assert attach_delivery(e,[trace])[0]['delivery']['state']=='sent'
    assert e.idle.queue_manual(SCOPE,number)['status']=='sent'
    assert len(p.calls)==1

@pytest.mark.parametrize('change',['scope','power','busy','expiry','new_message','profile'])
async def test_manual_cancellation_and_permissions(rig,change):
    e,s,clock,p=rig;number='a'*32
    if change=='scope':
        with pytest.raises(ValueError):e.idle.queue_manual('onebot:99999:group:'+GROUP,number)
        assert not p.calls;return
    e.idle.queue_manual(SCOPE,number)
    if change=='busy':
        with pytest.raises(ValueError,match='busy'):e.idle.queue_manual(SCOPE,'b'*32)
        return
    if change=='power':
        s.onebot_enabled=False;write_settings(e.root,s)
    elif change=='expiry':clock.advance(241)
    elif change=='new_message':await e.handle(activity())
    elif change=='profile':e._active_guard=lambda:False
    assert await e.idle.claim_manual(SELF)==[]
    assert not p.calls

async def test_manual_revocation_after_generation_and_failed_send_not_retried(rig):
    e,s,_,p=rig;e.idle.queue_manual(SCOPE,'a'*32)
    item,=await e.idle.claim_manual(SELF)
    s.onebot_allowed_groups=[];write_settings(e.root,s)
    assert not e.idle.authorize(item['id'],item['claim_token'],SELF)
    assert e.idle.ack(item['id'],item['claim_token'],SELF,'failed')['acknowledged']
    assert await e.idle.claim_manual(SELF)==[]
    assert len(p.calls)==1


def test_api_auth_offline_idempotency_and_status(rig,monkeypatch):
    e,s,_,_=rig;app=create_app(e.root)
    c=TestClient(app);headers={'Authorization':'Bearer '+token(e.root,'admin')}
    body={'scope':SCOPE,'request_id':'c'*32}
    assert c.post('/admin/groups/idle/trigger',json=body).status_code==401
    assert c.post('/admin/groups/idle/trigger',headers=headers,json=body).status_code==409
    monkeypatch.setattr(app.state.qq_connection,'snapshot',lambda:{'online':True})
    r=c.post('/admin/groups/idle/trigger',headers=headers,json=body);assert r.status_code==200,r.text
    assert c.post('/admin/groups/idle/trigger',headers=headers,json=body).json()==r.json()
    assert c.get('/admin/groups/idle/requests/'+'c'*32,headers=headers).json()['status']=='queued'
    assert c.post('/admin/groups/idle/trigger',headers=headers,json={**body,'force':True}).status_code==400
    app.state.engine.close();app.state.subscriptions.close()
