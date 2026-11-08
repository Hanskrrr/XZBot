import io
import json
import zipfile
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from azusa_bot import qq_runtime
from azusa_bot.app import create_app
from azusa_bot.external_runtime import connection_archive
from azusa_bot.settings import Settings, write_settings, private_write, token
from azusa_bot.snowluma_admin import SnowLumaAdmin
from azusa_bot.napcat_admin import NapCatError


def setup(tmp_path, adapter='snowluma'):
    write_settings(tmp_path,Settings(transport='onebot',onebot_self_id='10001'))
    qq_runtime.save(tmp_path,{'schema':1,'environment':'external','adapter':adapter})
    return tmp_path


def test_legacy_and_independent_account_choices(tmp_path,monkeypatch):
    monkeypatch.setattr('azusa_bot.external_runtime.platform.system',lambda:'Darwin')
    assert qq_runtime.read(tmp_path)['environment']=='lima'
    setup(tmp_path)
    other=tmp_path/'other';other.mkdir()
    assert qq_runtime.read(other)['adapter']=='napcat'
    for data in [{'schema':1,'environment':'native','adapter':'snowluma'},
                 {'schema':1,'environment':'external','adapter':'shell command'},
                 {'schema':True,'environment':'external','adapter':'napcat'}]:
        with pytest.raises(ValueError):qq_runtime.save(tmp_path,data)


def test_snowluma_zip_private_authenticated_loopback_only(tmp_path):
    setup(tmp_path)
    with zipfile.ZipFile(io.BytesIO(connection_archive(tmp_path))) as z:
        cfg=json.loads(z.read('onebot.json'))['networks']
    assert cfg['httpServers'][0]['host']=='127.0.0.1'
    assert cfg['httpServers'][0]['accessToken']==token(tmp_path,'onebot')
    assert cfg['wsClients'][0]['accessToken']==token(tmp_path,'onebot')
    assert cfg['wsClients'][0]['url']=='ws://127.0.0.1:6199/ws'
    assert token(tmp_path,'onebot') not in json.dumps(qq_runtime.describe(tmp_path))


@pytest.mark.parametrize('health,state',[({'online':True,'good':True},'online'),
    ({'online':False,'good':True},'offline'),({'online':'true','good':True},'unknown'),
    ({'online':True},'unknown'),({'online':True,'good':False},'unknown')])
async def test_snowluma_requires_real_identity_and_strict_health(tmp_path,monkeypatch,health,state):
    setup(tmp_path);calls=[];client=httpx.AsyncClient
    def handler(request):
        assert request.url.host=='127.0.0.1'
        assert request.headers['Authorization']=='Bearer '+token(tmp_path,'onebot')
        calls.append(request.url.path)
        value=health if request.url.path=='/get_status' else {'user_id':10001,'nickname':'fixture'}
        return httpx.Response(200,json={'status':'ok','retcode':0,'data':value})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client(**kw,transport=httpx.MockTransport(handler)))
    assert (await SnowLumaAdmin(tmp_path).status())['state']==state
    assert calls==['/get_status','/get_login_info']


async def test_snowluma_does_not_treat_webui_or_bad_identity_as_login(tmp_path,monkeypatch):
    setup(tmp_path);admin=SnowLumaAdmin(tmp_path)
    monkeypatch.setattr(admin,'_onebot',AsyncMock(side_effect=[{'online':True,'good':True},{'user_id':'nickname'}]))
    with pytest.raises(NapCatError,match='invalid_response'):await admin.status()
    with pytest.raises(NapCatError,match='desktop_login'):await admin.qr_svg()


def test_select_stops_runtime_and_clears_old_status(tmp_path,monkeypatch):
    setup(tmp_path,'napcat');stop=AsyncMock()
    monkeypatch.setattr('azusa_bot.bot_runtime.NativeBotRuntime.stop',stop)
    monkeypatch.setattr(qq_runtime,'local_listener_open',lambda port:False)
    app=create_app(tmp_path);m=app.state.qq_connection
    m.login={'state':'online'};m.login_at=123;m.bridge={'connected':True};m.bridge_at=123
    client=TestClient(app);headers={'Authorization':'Bearer '+token(tmp_path,'admin')}
    body={'schema':1,'environment':'external','adapter':'snowluma'}
    assert client.put('/admin/runtime/choice',json=body).status_code==401
    response=client.put('/admin/runtime/choice',headers=headers,json=body)
    assert response.status_code==200,response.text
    assert stop.await_count>=1
    assert app.state.bot_power.status()['state']=='stopped'
    assert isinstance(m.admin,SnowLumaAdmin) and m.login is None and m.bridge is None


def test_invalid_runtime_does_not_stop_a_working_bot(tmp_path,monkeypatch):
    setup(tmp_path,'napcat');stop=AsyncMock()
    monkeypatch.setattr('azusa_bot.bot_runtime.NativeBotRuntime.stop',stop)
    app=create_app(tmp_path);client=TestClient(app)
    response=client.put('/admin/runtime/choice',headers={'Authorization':'Bearer '+token(tmp_path,'admin')},
        json={'schema':1,'environment':'native','adapter':'snowluma'})
    assert response.status_code==400 and stop.await_count==0


def test_unverified_snowluma_marker_cannot_stop_working_account(tmp_path,monkeypatch):
    setup(tmp_path,'napcat')
    monkeypatch.setattr('platform.system',lambda:'Darwin')
    private_write(tmp_path/'runtime/snowluma-prepared.json',json.dumps({'schema':1,'root':'another-account','vm':'sl'}))
    stop=AsyncMock()
    monkeypatch.setattr('azusa_bot.bot_runtime.NativeBotRuntime.stop',stop)
    app=create_app(tmp_path);client=TestClient(app)
    result=client.put('/admin/runtime/choice',headers={'Authorization':'Bearer '+token(tmp_path,'admin')},
        json={'schema':1,'environment':'lima','adapter':'snowluma'})
    assert result.status_code==409 and stop.await_count==0
    assert not qq_runtime.describe(tmp_path)['prepared']['lima_snowluma']


def test_occupied_external_adapter_cannot_be_switched(tmp_path,monkeypatch):
    setup(tmp_path,'napcat')
    monkeypatch.setattr('azusa_bot.bot_runtime.NativeBotRuntime.stop',AsyncMock())
    monkeypatch.setattr(qq_runtime,'local_listener_open',lambda port:True)
    app=create_app(tmp_path);client=TestClient(app)
    result=client.put('/admin/runtime/choice',headers={'Authorization':'Bearer '+token(tmp_path,'admin')},
        json={'schema':1,'environment':'external','adapter':'snowluma'})
    assert result.status_code==409 and result.json()['detail']=='qq_runtime_previous_still_running'
    assert qq_runtime.read(tmp_path)['adapter']=='napcat'
    assert not app.state.bot_power.status()['enabled']


async def test_lima_refuses_second_adapter_for_same_account(tmp_path,monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
    import napcat_runtime
    from qq_runtime_action import assert_other_qq_stopped
    setup(tmp_path)
    monkeypatch.setattr('platform.system',lambda:'Darwin')
    qq_runtime.save(tmp_path,{'schema':1,'environment':'lima','adapter':'napcat'})
    lima_dir=tmp_path/'runtime/lima'
    for name in ['azusa','sl']:
        private_write(lima_dir/name/'lima.yaml','')
    private_write(lima_dir/'.azusa-project',str(tmp_path))
    monkeypatch.setattr(napcat_runtime,'LIMA_DIR',lima_dir)
    monkeypatch.setattr(napcat_runtime,'lima',lambda *a,**k:SimpleNamespace(stdout=json.dumps({'name':'sl','status':'Running'})))
    with pytest.raises(RuntimeError,match='other_adapter_running'):assert_other_qq_stopped(tmp_path)
    monkeypatch.setattr(napcat_runtime,'lima',lambda *a,**k:SimpleNamespace(stdout=json.dumps({'name':'sl','status':'Stopped'})))
    assert_other_qq_stopped(tmp_path)


def test_login_only_environment_is_closeable_without_enabling_replies(tmp_path,monkeypatch):
    setup(tmp_path,'napcat');monkeypatch.setattr('platform.system',lambda:'Darwin')
    qq_runtime.save(tmp_path,{'schema':1,'environment':'lima','adapter':'napcat'})
    calls=AsyncMock();monkeypatch.setattr('azusa_bot.bot_runtime.NativeBotRuntime._run',calls)
    app=create_app(tmp_path);app.state.bot_power._write('stopped')
    client=TestClient(app);headers={'Authorization':'Bearer '+token(tmp_path,'admin')}
    response=client.post('/admin/runtime/open-login',headers=headers,json={})
    assert response.status_code==200 and response.json()['reply_enabled'] is False
    assert app.state.bot_power.status()['state']=='error'
    response=client.post('/admin/bot-power/stop',headers=headers,json={})
    assert response.status_code==200
    assert [c.args for c in calls.await_args_list]==[('start',),('stop',)]


def test_runtime_config_migration_keeps_adapter_but_not_host(tmp_path,monkeypatch):
    from test_migration import make_source,fake_credentials
    from azusa_bot.migration import export_bundle,restore_bundle
    source=make_source(tmp_path)
    private_write(source/'config/qq-runtime.json',json.dumps({'schema':1,'environment':'lima','adapter':'snowluma'}))
    out=tmp_path/'private.zip';export_bundle(source,out,credential_reader=fake_credentials)
    with zipfile.ZipFile(out) as z:z.extractall(tmp_path/'extract')
    monkeypatch.setattr('azusa_bot.migration.platform.system',lambda:'Linux')
    restore_bundle(tmp_path/'extract/XZBot-muse',tmp_path/'restored')
    assert qq_runtime.read(tmp_path/'restored')=={'schema':1,'environment':'external','adapter':'snowluma'}
