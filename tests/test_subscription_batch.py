"""Atomic multi-recipient subscriptions using synthetic local responses only."""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from azusa_bot.app import create_app
from azusa_bot.bilibili import BilibiliSubscriptions, ROOM_ENDPOINT
from azusa_bot.settings import Settings, read_settings, write_settings


@pytest.fixture
def rig(tmp_path):
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id="10001", onebot_enabled=True,
        onebot_allowed_groups=["20001", "20002"], onebot_allowed_dm_users=["30001"]))
    clock=[1000.0]
    room={"room_id":123,"uid":456,"title":"合成直播","live_status":0,"live_time":"2026-01-01 12:00:00"}
    requests=[]
    def handler(request):
        assert request.url.host=="api.live.bilibili.com" and request.url.path==ROOM_ENDPOINT
        assert "cookie" not in request.headers and "authorization" not in request.headers
        requests.append(request)
        return httpx.Response(200,json={"code":0,"data":dict(room)})
    service=BilibiliSubscriptions(tmp_path,client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),clock=lambda:clock[0])
    return service,clock,room,requests


def body(**changes):
    return {"type":"bilibili_live","input_type":"room","value":"123",
        "targets":[{"kind":"group","target_id":"20001"},{"kind":"dm","target_id":"30001"}],
        "enabled":True,"include_image":False,**changes}


def database_state(service):
    with service.db() as db:
        return {table:[dict(row) for row in db.execute("SELECT * FROM "+table+" ORDER BY rowid")]
                for table in ("rooms","subscriptions","outbox")}


async def test_batch_resolves_once_and_existing_poller_fans_out_once_per_target(rig):
    service,clock,room,calls=rig
    result=await service.add_many(body())
    assert result["created_count"]==2 and result["existing_count"]==0
    assert {row["kind"] for row in result["subscriptions"]}=={"group","dm"}
    assert len(calls)==1
    room["live_status"]=1;clock[0]+=91
    await service.poll_once()
    assert len(calls)==2
    deliveries=service.fetch_claim("10001")
    assert {(row["kind"],row["target_id"]) for row in deliveries}=={("group","20001"),("dm","30001")}
    for row in deliveries:
        assert service.ack(row["id"],row["claim_token"],"sent")["acknowledged"]
    clock[0]+=91;await service.poll_once()
    assert service.fetch_claim("10001")==[]


async def test_initial_live_session_is_only_baseline_for_every_new_target(rig):
    service,clock,room,_=rig
    room["live_status"]=1
    result=await service.add_many(body())
    assert all(row["last_session"]=="123:2026-01-01 12:00:00" for row in result["subscriptions"])
    clock[0]+=91;await service.poll_once()
    assert service.fetch_claim("10001")==[]


@pytest.mark.parametrize("change",[
    {"targets":[]}, {"targets":"20001"}, {"targets":[{"kind":"group","target_id":"20001","self_id":"other"}]},
    {"targets":[{"kind":"group","target_id":"20001"},{"kind":"dm","target_id":"99999"}]},
    {"targets":[{"kind":"group","target_id":True}]},
    {"targets":[{"kind":[],"target_id":"20001"}]},
    {"targets":[{"kind":{},"target_id":"20001"}]},
    {"targets":[{"kind":"group","target_id":"20001"}]*51},
    {"enabled":"yes"}, {"include_image":1}, {"self_id":"99999"}, {"type":[]}, {"input_type":{}},
])
async def test_invalid_batch_rejected_before_source_request_without_partial_writes(rig,change):
    service,_,_,calls=rig
    with pytest.raises(ValueError):await service.add_many(body(**change))
    assert not calls and database_state(service)=={"rooms":[],"subscriptions":[],"outbox":[]}


async def test_duplicate_targets_and_repeated_request_preserve_existing_rows_and_outbox(rig):
    service,clock,room,calls=rig
    repeated=body(targets=[{"kind":"group","target_id":"20001"},{"kind":"group","target_id":20001},
        {"kind":"dm","target_id":"30001"}])
    result=await service.add_many(repeated)
    assert result["created_count"]==2 and result["existing_count"]==0
    room["live_status"]=1;clock[0]+=91;await service.poll_once()
    service.fetch_claim("10001")
    old=database_state(service)
    again=await service.add_many(repeated|{"enabled":False,"include_image":True})
    assert again=={"created_count":0,"existing_count":2,"subscriptions":[]}
    assert database_state(service)==old


async def test_adding_target_preserves_paused_sibling_and_existing_source_schedule(rig):
    service,clock,room,_=rig
    old=await service.add({"type":"bilibili_live","input_type":"room","value":"123",
        "kind":"group","target_id":"20001","enabled":False,"include_image":False})
    before=database_state(service)
    room["live_status"]=1;clock[0]+=12
    result=await service.add_many(body(include_image=True))
    assert result["created_count"]==1 and result["existing_count"]==1
    assert result["subscriptions"][0]["last_session"]=="123:2026-01-01 12:00:00"
    assert result["subscriptions"][0]["include_image"] is True
    after=database_state(service)
    assert before["rooms"]==after["rooms"]
    assert next(row for row in before["subscriptions"] if row["id"]==old["id"])==next(row for row in after["subscriptions"] if row["id"]==old["id"])


async def test_existing_steam_candidate_and_poll_deadline_survive_adding_target(rig,monkeypatch):
    service,_,_,_=rig
    info={"type":"steam","source_id":"76561198000000001","room_id":"steam:76561198000000001",
        "uid":"76561198000000001","name":"合成玩家","title":"game A","live_status":1,"session":"A","gameid":"1"}
    async def resolve(_):return dict(info)
    monkeypatch.setattr(service,"resolve",resolve)
    payload=body(type="steam",input_type="steamid",value=info["uid"],targets=[{"kind":"group","target_id":"20001"}])
    await service.add_many(payload)
    with service.db() as db:
        db.execute("UPDATE rooms SET payload=?,next_poll=?,last_check=?",(json.dumps({"candidate_session":"B","gameid":"1"}),4321,1234))
    old=database_state(service)
    info.update(session="B",title="game B",gameid="2")
    result=await service.add_many(payload|{"targets":[{"kind":"dm","target_id":"30001"}]})
    assert result["created_count"]==1 and result["subscriptions"][0]["last_session"]=="B"
    assert database_state(service)["rooms"]==old["rooms"]
    assert database_state(service)["subscriptions"][0]==old["subscriptions"][0]


async def test_capacity_counts_new_targets_and_failure_is_atomic(rig,monkeypatch):
    service,_,_,_=rig
    monkeypatch.setattr("azusa_bot.bilibili.MAX_SUBSCRIPTIONS",2)
    await service.add_many(body(targets=[{"kind":"group","target_id":"20001"}]))
    before=database_state(service)
    with pytest.raises(ValueError,match="bilibili_subscription_limit"):
        await service.add_many(body(targets=[{"kind":"group","target_id":"20001"},
            {"kind":"group","target_id":"20002"},{"kind":"dm","target_id":"30001"}]))
    assert database_state(service)==before
    result=await service.add_many(body())
    assert result["created_count"]==1 and result["existing_count"]==1
    assert (await service.add_many(body()))["existing_count"]==2


@pytest.mark.parametrize("change",[{"onebot_self_id":"99999"},{"transport":"official"},{"onebot_allowed_dm_users":[]}])
async def test_policy_change_during_network_cannot_partially_create(rig,monkeypatch,change):
    service,_,_,_=rig
    real_resolve=service.resolve;entered,release=asyncio.Event(),asyncio.Event()
    async def blocked(request):
        entered.set();await release.wait();return await real_resolve(request)
    monkeypatch.setattr(service,"resolve",blocked)
    pending=asyncio.create_task(service.add_many(body()))
    await entered.wait()
    write_settings(service.root,read_settings(service.root).model_copy(update=change))
    release.set()
    with pytest.raises(ValueError):await pending
    assert database_state(service)=={"rooms":[],"subscriptions":[],"outbox":[]}


async def test_authorization_rechecked_after_network_and_database_failure_rolls_back(rig,monkeypatch):
    service,_,_,_=rig
    allowed=[True]
    def authorize():
        if not allowed[0]:raise PermissionError("synthetic_revocation")
    real_resolve=service.resolve
    async def revoked(request):
        result=await real_resolve(request);allowed[0]=False;return result
    monkeypatch.setattr(service,"resolve",revoked)
    with pytest.raises(PermissionError):await service.add_many(body(),authorize=authorize)
    assert not service.list()
    monkeypatch.setattr(service,"resolve",real_resolve)
    with service.db() as db:
        db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON subscriptions WHEN NEW.kind='dm' BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    with pytest.raises(sqlite3.IntegrityError):await service.add_many(body())
    assert database_state(service)=={"rooms":[],"subscriptions":[],"outbox":[]}


async def test_two_service_objects_idempotently_add_same_batch(rig):
    service,clock,_,_=rig
    other=BilibiliSubscriptions(service.root,client=service.client,clock=lambda:clock[0])
    results=await asyncio.gather(service.add_many(body()),other.add_many(body()))
    assert sorted(row["created_count"] for row in results)==[0,2]
    assert len(service.list())==2


def test_batch_admin_endpoint_requires_admin_and_returns_only_created_rows(rig):
    service,_,_,calls=rig
    (service.root/"content").mkdir();(service.root/"content/persona.md").write_text("合成测试角色")
    app=create_app(service.root)
    app.state.subscriptions.client=service.client
    admin={"Authorization":"Bearer "+(service.root/"data/admin.token").read_text().strip()}
    bridge={"Authorization":"Bearer "+(service.root/"data/bridge.token").read_text().strip()}
    try:
        with TestClient(app) as client:
            assert client.post("/admin/subscriptions/batch",json=body()).status_code==401
            assert client.post("/admin/subscriptions/batch",json=body(),headers=bridge).status_code==401
            assert not calls
            result=client.post("/admin/subscriptions/batch",json=body(enabled=False),headers=admin)
            assert result.status_code==200 and result.json()["created_count"]==2
            assert all(not row["enabled"] for row in result.json()["subscriptions"])
            again=client.post("/admin/subscriptions/batch",json=body(enabled=True),headers=admin)
            assert again.status_code==200 and again.json()=={"created_count":0,"existing_count":2,"subscriptions":[]}
    finally:
        app.state.engine.close()
