"""Live polling/outbox behavior using synthetic public API responses only."""
import asyncio
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from azusa_bot.bilibili import BilibiliSubscriptions,BilibiliError,BASE,ROOM_ENDPOINT,UID_ENDPOINT
from azusa_bot.settings import Settings,write_settings,read_settings


@pytest.fixture
def rig(tmp_path):
    now=[1000.]
    room={'room_id':123,'uid':456,'title':'合成测试直播','live_status':0,'live_time':'0000-00-00 00:00:00'}
    calls=[];failure=[None]
    def handler(request):
        calls.append(request)
        assert request.url.host=='api.live.bilibili.com'
        assert 'Cookie' not in request.headers and 'Authorization' not in request.headers
        if failure[0]:return httpx.Response(failure[0],text='secret-not-to-log')
        data={'456':{'room_id':123,'uname':'测试主播'}} if request.url.path==UID_ENDPOINT else dict(room)
        return httpx.Response(200,json={'code':0,'data':data})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
    settings=Settings(transport='onebot',onebot_self_id='10001',onebot_enabled=True,
                      onebot_allowed_groups=['20001','20002'],onebot_allowed_dm_users=['30001'])
    write_settings(tmp_path,settings)
    service=BilibiliSubscriptions(tmp_path,client=client,clock=lambda:now[0])
    yield service,now,room,calls,failure


def body(**kw):
    return {'input_type':'room','value':'https://live.bilibili.com/123?from=share',
            'kind':'group','target_id':'20001','enabled':True,**kw}


async def live(rig,stamp='2026-11-06 20:00:00'):
    service,now,room,*_=rig
    room.update(live_status=1,live_time=stamp);now[0]+=91
    await service.poll_once()


@pytest.mark.asyncio
async def test_uid_and_room_resolve_sources_without_cookie_or_arbitrary_url(rig):
    service,now,room,calls,_=rig
    result=await service.resolve({'input_type':'uid','value':'456'})
    assert result['room_id']=='123' and result['uid']=='456' and result['name']=='测试主播'
    assert [r.url.path for r in calls]==[UID_ENDPOINT,ROOM_ENDPOINT]
    assert dict(calls[0].url.params)=={'uids[]':'456'}
    assert (await service.resolve({'input_type':'room','value':'123'}))['url']=='https://live.bilibili.com/123'


@pytest.mark.asyncio
@pytest.mark.parametrize('value',['https://evil.example/123','http://127.0.0.1/123','https://live.bilibili.com@evil.example/123','https://name:pass@live.bilibili.com/123','https://live.bilibili.com:443/123','https://live.bilibili.com/../123','00123','https://b23.tv/abc'])
async def test_source_rejects_unsafe_or_ambiguous_addresses_before_request(rig,value):
    service,_,_,calls,_=rig
    with pytest.raises(ValueError):await service.resolve({'input_type':'room','value':value})
    assert not calls


@pytest.mark.asyncio
async def test_initial_live_baseline_and_restart_do_not_notify_current_session(rig):
    service,now,room,calls,_=rig
    room.update(live_status=1,live_time='2026-11-06 19:00:00')
    await service.add(body())
    now[0]+=91;await service.poll_once()
    restarted=BilibiliSubscriptions(service.root,client=service.client,clock=lambda:now[0])
    now[0]+=91;await restarted.poll_once()
    assert restarted.fetch_claim('10001')==[]
    assert service.list()[0]['last_session']=='123:2026-11-06 19:00:00'


@pytest.mark.asyncio
async def test_live_transition_claim_ack_is_single_delivery_and_restart_safe(rig):
    service,now,room,_,_=rig
    sub=await service.add(body())
    await live(rig)
    items=service.fetch_claim('10001');assert len(items)==1
    item=items[0]
    assert service.authorize_claim(item['id'],item['claim_token'],'10001')
    assert 'https://live.bilibili.com/123' in item['text'] and item['kind']=='group'
    assert service.fetch_claim('10001')==[]
    assert service.ack(item['id'],item['claim_token'],'sent')['acknowledged']
    now[0]+=91;await service.poll_once()
    restarted=BilibiliSubscriptions(service.root,client=service.client,clock=lambda:now[0])
    assert restarted.fetch_claim('10001')==[]
    assert restarted.status()['deliveries'][0]['status']=='sent'
    assert 'claim_token' not in json.dumps(restarted.status())


@pytest.mark.asyncio
async def test_uncertain_and_abandoned_claims_are_never_replayed(rig):
    service,now,*_=rig
    await service.add(body());await live(rig)
    item=service.fetch_claim('10001')[0]
    now[0]+=121
    assert service.status()['deliveries'][0]['status']=='uncertain'
    assert not service.authorize_claim(item['id'],item['claim_token'],'10001')
    assert not service.ack(item['id'],item['claim_token'],'sent')['acknowledged']
    assert service.fetch_claim('10001')==[]


@pytest.mark.asyncio
async def test_same_room_polled_once_and_separate_targets_receive_once(rig):
    service,now,room,calls,_=rig
    await service.add(body());await service.add(body(kind='dm',target_id='30001'))
    before=len(calls);await live(rig)
    assert len(calls)==before+1
    items=service.fetch_claim('10001')
    assert len(items)==2 and {x['target_id'] for x in items}=={'20001','30001'}
    await service.poll_once();assert len(calls)==before+1


@pytest.mark.asyncio
async def test_looping_not_live_and_new_session_while_live_is_detected(rig):
    service,now,room,*_=rig
    room.update(live_status=2);await service.add(body());await live(rig)
    item=service.fetch_claim('10001')[0];service.ack(item['id'],item['claim_token'],'uncertain')
    await live(rig,'2026-11-06 21:00:00')
    assert len(service.fetch_claim('10001'))==1


@pytest.mark.asyncio
async def test_disable_or_delete_revokes_pending_and_claimed_notifications(rig):
    service,now,*_=rig
    sub=await service.add(body());await live(rig)
    claimed=service.fetch_claim('10001')[0]
    await service.update(sub['id'],{'enabled':False})
    assert not service.authorize_claim(claimed['id'],claimed['claim_token'],'10001')
    assert service.fetch_claim('10001')==[]
    await service.update(sub['id'],{'enabled':True})
    now[0]+=91;await service.poll_once();assert service.fetch_claim('10001')==[]
    await live(rig,'2026-11-06 22:00:00')
    service.delete(sub['id']);assert service.fetch_claim('10001')==[]
    assert not service.list()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'onebot_enabled':False},{'onebot_self_id':'99999'},{'onebot_allowed_groups':[]},{'transport':'official'}])
async def test_current_policy_revokes_notification_at_claim_and_authorize(rig,change):
    service,now,*_=rig
    await service.add(body());await live(rig)
    item=service.fetch_claim('10001')[0]
    settings=read_settings(service.root)
    for key,value in change.items():setattr(settings,key,value)
    write_settings(service.root,settings)
    assert not service.authorize_claim(item['id'],item['claim_token'],'10001')
    assert service.fetch_claim('10001')==[]


@pytest.mark.asyncio
async def test_account_claim_does_not_steal_other_account_and_ttl_expires(rig):
    service,now,*_=rig
    await service.add(body());await live(rig)
    assert service.fetch_claim('99999')==[]
    assert service.status()['pending']==1
    now[0]+=901
    assert service.fetch_claim('10001')==[]
    assert service.status()['deliveries'][0]['status']=='expired'


@pytest.mark.asyncio
async def test_error_backoff_and_unknown_live_time_fail_closed(rig,caplog):
    service,now,room,calls,failure=rig
    sub=await service.add(body())
    failure[0]=412;now[0]+=91;await service.poll_once()
    count=len(calls)
    assert service.status()['last_error']=='bilibili_http_412'
    now[0]+=100;await service.poll_once();assert len(calls)==count
    failure[0]=None;room.update(live_status=1,live_time='not-a-time');now[0]+=100;await service.poll_once()
    assert service.status()['last_error']=='bilibili_live_session_unavailable'
    assert service.fetch_claim('10001')==[]
    assert 'secret-not-to-log' not in caplog.text


@pytest.mark.asyncio
async def test_empty_or_disabled_library_makes_no_background_requests(rig):
    service,now,room,calls,_=rig
    await service.poll_once();assert calls==[]
    await service.add(body(enabled=False));count=len(calls)
    now[0]+=1000;await service.poll_once();assert len(calls)==count


@pytest.mark.asyncio
async def test_baseline_failure_cannot_enable_subscription_and_update_is_atomic(rig):
    service,now,room,calls,failure=rig
    sub=await service.add(body(enabled=False))
    failure[0]=500
    with pytest.raises(BilibiliError):await service.update(sub['id'],{'enabled':True})
    assert not service.list()[0]['enabled']
    with pytest.raises(BilibiliError):await service.add(body(target_id='20002'))
    assert len(service.list())==1


@pytest.mark.asyncio
async def test_backup_restores_subscriptions_and_deduplication(rig,tmp_path):
    service,now,*_=rig
    await service.add(body());await live(rig)
    item=service.fetch_claim('10001')[0];service.ack(item['id'],item['claim_token'],'sent')
    path=tmp_path/'backup.sqlite3';service.backup(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0]==1
        assert db.execute('SELECT status FROM outbox').fetchone()[0]=='sent'
    assert path.stat().st_mode&0o777==0o600


@pytest.mark.asyncio
async def test_forbidden_target_and_extra_fields_fail_before_network(rig):
    service,now,room,calls,_=rig
    for request in [body(target_id='999'),body(self_id='evil'),body(enabled='yes')]:
        with pytest.raises(ValueError):await service.add(request)
    assert calls==[]


@pytest.mark.asyncio
async def test_transport_rejects_redirects_and_oversized_body(rig):
    service,*_=rig
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':'http://127.0.0.1/private'})),follow_redirects=False) as client:
        service.client=client
        with pytest.raises(BilibiliError,match='^bilibili_http_302$'):await service.resolve({'input_type':'room','value':'123'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'x'*(1024*1024+1)))) as client:
        service.client=client
        with pytest.raises(BilibiliError,match='^bilibili_response_too_large$'):await service.resolve({'input_type':'room','value':'123'})


@pytest.mark.asyncio
async def test_one_target_is_throttled_even_for_uncertain_claims_and_same_batch(rig):
    service,now,room,_,_=rig
    first=await service.add(body())
    # Inject a second independent room through the mocked public response.
    room.update(room_id=124,uid=457)
    await service.add(body(value='124'))
    with service.db() as db:
        for sub in db.execute('SELECT * FROM subscriptions').fetchall():
            import uuid
            db.execute("INSERT INTO outbox(id,subscription_id,room_id,session,self_id,kind,target_id,revision,text,status,claim_token,created_at,claimed_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,?,NULL,NULL)",
                (uuid.uuid4().hex,sub['id'],sub['room_id'],'session-'+sub['room_id'],sub['self_id'],sub['kind'],sub['target_id'],sub['revision'],'开播提醒',now[0]))
    batch=service.fetch_claim('10001',limit=5)
    assert len(batch)==1 and service.status()['pending']==1
    item=batch[0];service.ack(item['id'],item['claim_token'],'uncertain')
    now[0]+=29;assert service.fetch_claim('10001')==[]
    now[0]+=1;assert len(service.fetch_claim('10001'))==1


@pytest.mark.asyncio
async def test_pause_still_available_after_whitelist_removal_or_account_switch(rig):
    service,*_=rig
    sub=await service.add(body());await live(rig)
    item=service.fetch_claim('10001')[0]
    settings=read_settings(service.root);settings.onebot_self_id='99999';settings.onebot_allowed_groups=[]
    write_settings(service.root,settings)
    assert not (await service.update(sub['id'],{'enabled':False}))['enabled']
    assert not service.authorize_claim(item['id'],item['claim_token'],'10001')


@pytest.mark.asyncio
async def test_ended_session_expires_waiting_notification(rig):
    service,now,room,*_=rig
    await service.add(body());await live(rig)
    assert service.status()['pending']==1
    room.update(live_status=0);now[0]+=91;await service.poll_once()
    assert service.fetch_claim('10001')==[]
    assert service.status()['deliveries'][0]['status']=='expired'


@pytest.mark.asyncio
async def test_pause_does_not_wait_for_inflight_public_poll(rig):
    service,now,room,*_=rig
    sub=await service.add(body());now[0]+=91
    entered,release=asyncio.Event(),asyncio.Event()
    original=service._room
    async def waiting(*args,**kwargs):
        entered.set();await release.wait()
        return await original(*args,**kwargs)
    service._room=waiting
    task=asyncio.create_task(service.poll_once())
    await asyncio.wait_for(entered.wait(),1)
    result=await asyncio.wait_for(service.update(sub['id'],{'enabled':False}),.2)
    assert not result['enabled']
    release.set();await task
    assert service.fetch_claim('10001')==[]
