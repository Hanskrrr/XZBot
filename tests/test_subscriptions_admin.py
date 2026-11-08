"""Local subscription UI API to durable outbox, without QQ or public requests."""
import asyncio,json
import httpx
import pytest
from fastapi.testclient import TestClient
from azusa_bot.app import create_app,backup_project
from azusa_bot.settings import Settings,write_settings


@pytest.fixture
def appdata(tmp_path):
    root=tmp_path/'bot';(root/'content/lore').mkdir(parents=True)
    (root/'content/persona.md').write_text('这是合成测试角色。')
    write_settings(root,Settings(transport='onebot',onebot_self_id='100001',onebot_enabled=True,onebot_allowed_groups=['300003']))
    app=create_app(root);sub=app.state.subscriptions
    clock=[1000.];sub.clock=lambda:clock[0]
    data={'room_id':123,'uid':456,'title':'合成开播测试','live_status':0,'live_time':'2026-01-01 12:00:00'}
    def handler(request):
        assert str(request.url).startswith('https://api.live.bilibili.com/room/v1/Room/get_info')
        return httpx.Response(200,json={'code':0,'data':dict(data)})
    sub.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    admin={'Authorization':'Bearer '+(root/'data/admin.token').read_text()}
    bridge={'Authorization':'Bearer '+(root/'data/bridge.token').read_text()}
    with TestClient(app) as client:
        yield root,app,client,sub,admin,bridge,data,clock
    asyncio.run(sub.client.aclose());app.state.engine.memory.close();app.state.engine.groups.close()


def test_admin_configuration_and_bridge_delivery_boundaries(appdata):
    root,app,c,sub,admin,bridge,data,clock=appdata
    assert c.get('/admin/subscriptions').status_code==401
    assert c.get('/admin/subscriptions',headers=bridge).status_code==401
    assert c.get('/admin/diagnostics',headers=bridge).status_code==401
    assert c.post('/bridge/notifications/claim',headers=admin,json={'self_id':'100001'}).status_code==401
    payload={'input_type':'room','value':'123','kind':'group','target_id':'300003','enabled':True}
    r=c.post('/admin/subscriptions',headers=admin,json=payload);assert r.status_code==200
    number=r.json()['id']
    assert c.get('/admin/subscriptions',headers=admin).json()['status']['enabled']==1
    assert c.post('/bridge/notifications/claim',headers=bridge,json={'self_id':'100001'}).json()['notifications']==[]
    data['live_status']=1;clock[0]+=91
    asyncio.run(sub.poll_once())
    row=c.post('/bridge/notifications/claim',headers=bridge,json={'self_id':'100001'}).json()['notifications'][0]
    auth={'id':row['id'],'claim_token':row['claim_token'],'self_id':'100001'}
    assert c.post('/bridge/notifications/authorize',headers=bridge,json=auth).json()['allowed']
    assert c.patch('/admin/subscriptions/'+number,headers=admin,json={'enabled':False}).status_code==200
    assert not c.post('/bridge/notifications/authorize',headers=bridge,json=auth).json()['allowed']
    assert c.post('/bridge/notifications/ack',headers=bridge,json=auth|{'status':'sent'}).json()['acknowledged'] is False
    assert c.delete('/admin/subscriptions/'+number,headers=admin).status_code==200
    assert c.get('/admin/subscriptions',headers=admin).json()['subscriptions']==[]


def test_diagnostics_and_subscription_backup(appdata):
    root,app,c,sub,admin,bridge,data,clock=appdata
    app.state.engine.diagnostics.record('llm','deepseek_http_503')
    r=c.get('/admin/diagnostics',headers=admin)
    assert r.status_code==200 and r.json()['services'][0]['code']=='deepseek_http_503'
    c.post('/admin/subscriptions',headers=admin,json={'input_type':'room','value':'123','kind':'group','target_id':'300003'})
    backup=backup_project(root,app.state.engine)
    import sqlite3
    with sqlite3.connect(backup/'bilibili.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0]==1
    assert c.post('/admin/subscriptions',headers=admin,json={'input_type':'room','value':'https://evil.example/123','kind':'group','target_id':'300003'}).status_code==400
    assert c.post('/bridge/notifications/claim',headers=bridge,json={'self_id':True}).status_code==400


def test_new_admin_routes_never_accept_bridge_credentials(appdata):
    _,_,c,_,admin,bridge,_,_=appdata
    for path in ('/admin/gemini','/admin/subscription-credentials','/admin/desktop/status'):
        assert c.get(path,headers=bridge).status_code==401
        assert c.get(path).status_code==401
    for path in ('/admin/gemini/start','/admin/gemini/install','/admin/subscriptions/preview'):
        assert c.post(path,headers=bridge,json={}).status_code==401
    assert c.get('/admin/gemini',headers=admin).json()['credentials']['key_count']==0


def test_card_preview_stays_local_and_protects_media(appdata):
    _,_,c,sub,admin,bridge,_,_=appdata
    r=c.post('/admin/subscriptions/preview',headers=admin,json={
        'type':'bilibili_live','input_type':'room','value':'123','include_image':True})
    assert r.status_code==200
    result=r.json()
    assert result['preview_only'] and 'image_file' not in result
    assert result['media_url'].startswith('/admin/media/')
    assert c.get(result['media_url'],headers=bridge).status_code==401
    picture=c.get(result['media_url'],headers=admin)
    assert picture.status_code==200 and picture.content.startswith(b'\x89PNG')
    assert not sub.list() and sub.status()['pending']==0


def test_subscription_credentials_only_report_presence(appdata):
    _,_,c,_,admin,bridge,_,_=appdata
    secret='synthetic-steam-key-for-api-test'
    assert c.put('/admin/subscription-credentials',headers=bridge,json={'steam_api_key':secret}).status_code==401
    result=c.put('/admin/subscription-credentials',headers=admin,json={'steam_api_key':secret})
    assert result.status_code==200 and secret not in result.text
    assert result.json()['fields']['steam_api_key']['configured']
    result=c.get('/admin/subscription-credentials',headers=admin)
    assert secret not in result.text
    assert c.put('/admin/subscription-credentials',headers=admin,json={'clear':['steam_api_key']}).status_code==200
