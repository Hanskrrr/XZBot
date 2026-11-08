"""No QQ/network: stale sessions and transports must never become green online."""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from azusa_bot.app import create_app
from azusa_bot.napcat_admin import NapCatAdmin, NapCatError
from azusa_bot.qq_connection import QQConnection
from azusa_bot.settings import Settings, write_settings


@pytest.fixture
def monitor(tmp_path,monkeypatch):
    write_settings(tmp_path,Settings(transport='onebot',onebot_self_id='10001',onebot_enabled=True))
    clock=[1000.0];value={'available':True,'state':'online','reason_code':'online','qq_number':'10001','checked_at':clock[0]}
    calls=[]
    async def status(self):
        calls.append(1)
        if isinstance(value.get('error'),Exception):raise value['error']
        return dict(value,checked_at=clock[0])
    monkeypatch.setattr(NapCatAdmin,'status',status)
    return QQConnection(tmp_path,clock=lambda:clock[0],monotonic=lambda:clock[0]),clock,value,calls


async def test_login_alone_is_not_online_and_fresh_transport_is_required(monitor):
    m,clock,_,_=monitor
    assert (await m.check())['reason_code']=='bridge_stale'
    m.report('10001',True,True)
    assert (await m.check())['state']=='online'
    clock[0]+=46
    assert (await m.check())['reason_code']=='bridge_stale'
    m.report('10001',False,None)
    assert (await m.check())['state']=='disconnected'
    m.report('10001',True,False)
    assert (await m.check())['state']=='offline'
    m.report('10001',True,None)
    assert (await m.check())['reason_code']=='bridge_status_unknown'
    assert len(m.events)==6


async def test_login_revocation_wins_over_connected_transport_and_no_login_side_effect(monitor):
    m,clock,value,calls=monitor;m.report('10001',True,True)
    assert (await m.check())['state']=='online'
    value.update(state='offline',reason_code='qq_offline');clock[0]+=11
    assert (await m.check())['state']=='offline'
    value.update(state='reconnecting',reason_code='reconnecting');clock[0]+=11
    assert (await m.check())['state']=='reconnecting'
    value.update(state='online',reason_code='online',qq_number='10002');clock[0]+=11
    assert (await m.check())['reason_code']=='account_mismatch'
    assert len(calls)==4


async def test_failed_or_stale_reads_never_reuse_online_and_only_transitions_persist(monitor):
    m,clock,value,calls=monitor;m.report('10001',True,True)
    await asyncio.gather(m.check(),m.check(),m.check());assert len(calls)==1
    before=m.path.read_bytes();clock[0]+=5;await m.check();assert m.path.read_bytes()==before
    clock[0]+=31;assert m.snapshot()['reason_code']=='status_stale'
    value['error']=NapCatError('napcat_unavailable')
    assert (await m.check())['state']=='unavailable'
    records=json.loads(m.path.read_text());assert all(set(r)=={'at','state','reason_code'} for r in records)
    assert m.path.stat().st_mode & 0o077==0
    restored=QQConnection(m.root,clock=lambda:clock[0],monotonic=lambda:clock[0])
    assert restored.events==records and restored.snapshot()['online'] is False


@pytest.mark.parametrize('args',[('other',True,True),('10001','true',True),('10001',False,True),('10001',True,'true')])
def test_wrong_account_and_invalid_reports_rejected(monitor,args):
    m,*_=monitor
    with pytest.raises(ValueError):m.report(*args)
    assert m.bridge is None


async def test_snapshot_isolated_per_account_and_no_probes_for_official_transport(monitor,tmp_path):
    m,clock,_,calls=monitor;m.report('10001',True,True);await m.check()
    other=tmp_path/'other';write_settings(other,Settings(transport='official'))
    second=QQConnection(other)
    assert (await second.check())['state']=='not_applicable' and len(calls)==1


def test_status_routes_separate_admin_and_bridge_auth(monitor):
    m,*_=monitor;app=create_app(m.root)
    app.state.qq_connection.clock=m.clock
    app.state.qq_connection.monotonic=m.monotonic
    client=TestClient(app)
    admin={'Authorization':'Bearer '+(m.root/'data/admin.token').read_text().strip()}
    bridge={'Authorization':'Bearer '+(m.root/'data/bridge.token').read_text().strip()}
    payload={'self_id':'10001','connected':True,'online':True}
    assert client.get('/admin/qq-status',headers=bridge).status_code==401
    assert client.post('/bridge/connection-status',headers=admin,json=payload).status_code==401
    assert client.post('/bridge/connection-status',headers=bridge,json=payload|{'text':'forbidden'}).status_code==400
    assert client.post('/bridge/connection-status',headers=bridge,json=payload|{'self_id':'10002'}).status_code==400
    assert client.post('/bridge/connection-status',headers=bridge,json=payload).status_code==200
    assert client.get('/admin/qq-status',headers=admin).json()['state']=='online'


async def test_sleep_wall_clock_advance_expires_even_if_monotonic_pauses(monitor):
    m,clock,_,calls=monitor
    m.monotonic=lambda:1000.0
    m.report('10001',True,True);assert (await m.check())['state']=='online'
    clock[0]+=3600
    assert m.snapshot()['reason_code']=='status_stale'
    assert (await m.check())['reason_code']=='bridge_stale' and len(calls)==2


async def test_management_rate_limit_is_not_recorded_as_qq_logout(monitor):
    m,clock,value,_=monitor;m.report('10001',True,True);await m.check()
    clock[0]+=11;value['error']=NapCatError('napcat_webui_rate_limited')
    result=await m.check()
    assert result['state']=='unavailable' and result['reason_code']=='napcat_webui_rate_limited'
    assert '不代表 QQ 被踢' in result['message']
    assert m.events[-1]['reason_code']=='napcat_webui_rate_limited'


async def test_login_failure_category_survives_recovery_and_restart(monitor):
    m,clock,value,_=monitor
    value.update(state='login_required',reason_code='login_required',login_error_code='session_expired')
    assert (await m.check())['events'][0]['login_error_code']=='session_expired'
    clock[0]+=11;value.update(state='online',reason_code='online');value.pop('login_error_code')
    m.report('10001',True,True);await m.check()
    restored=QQConnection(m.root,clock=lambda:clock[0],monotonic=lambda:clock[0])
    assert any(r.get('login_error_code')=='session_expired' for r in restored.events)
    assert any('登录态失效' in r['message'] for r in restored.snapshot()['events'])


async def test_receipt_failures_override_online_without_mistaking_quiet_for_logout(monitor):
    m,clock,_,_=monitor;m.report('10001',True,True)
    assert (await m.check())['state']=='online'
    path=m.root/'data/state.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE deliveries(scope TEXT,status TEXT,at REAL); CREATE TABLE group_idle_attempts(scope TEXT,status TEXT,at REAL);')
        db.executemany('INSERT INTO deliveries VALUES (?,?,?)',[
            ('onebot:10001:dm:20001','demo',999),
            ('onebot:99999:dm:20001','uncertain',999)])
        db.executemany('INSERT INTO group_idle_attempts VALUES (?,?,?)',[
            ('onebot:10001:group:30001','uncertain',998),
            ('onebot:10001:group:30001','uncertain',999)])
    clock[0]+=6;m.report('10001',True,True)
    result=await m.check()
    assert result['state']=='degraded' and result['online'] is False
    assert result['traffic']['consecutive_unconfirmed']==2
    assert result['traffic']['last_received_at'] is None
    # A later heartbeat or actual incoming message does not prove outgoing works.
    with sqlite3.connect(path) as db:db.execute('INSERT INTO deliveries VALUES (?,?,?)',('onebot:10001:dm:20001','observed',1005))
    clock[0]+=6;m.report('10001',True,True)
    assert (await m.check())['state']=='degraded'
    with sqlite3.connect(path) as db:db.execute('INSERT INTO deliveries VALUES (?,?,?)',('onebot:10001:dm:20001','sent',1011))
    clock[0]+=6;m.report('10001',True,True)
    result=await m.check()
    assert result['state']=='online' and result['traffic']['consecutive_unconfirmed']==0
    assert result['traffic']['last_successful_request_at']==1011
