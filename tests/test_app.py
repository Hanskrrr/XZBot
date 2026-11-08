"""HTTP integration in isolated temp roots; no live accounts or network calls."""

import json
import concurrent.futures
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from support_content import seed_content

from azusa_bot.app import create_app
from azusa_bot.core import Engine
from azusa_bot.secrets import Credentials
from azusa_bot.settings import Owner, Settings, private_write, read_settings, write_settings


DEMO = "demo:group:g1"


class OfflineProvider:
    async def complete(self, messages, *, summary=False):
        return {"memories": []} if summary else {"text": "已完成本机测试。", "mode": "text"}

    async def synthesize(self, text):
        raise AssertionError("HTTP tests must not synthesize audio")


@pytest.fixture
def http(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    seed_content(root)
    (root / "ui").mkdir()
    (root / "ui/index.html").write_text("<!doctype html><title>Test</title>")
    write_settings(root, Settings(min_interval_seconds=0))
    # Never read actual credentials or use a real provider from the machine.
    monkeypatch.setattr("azusa_bot.app.load_credentials", lambda _: Credentials(
        deepseek_key="test-only-private-deepseek-value", fish_key="test-only-private-fish-value",
        fish_reference="test-only-private-reference-value", qq_app_id="testapp",
        qq_secret="test-only-private-qq-value"))
    app = create_app(root)
    app.state.engine.provider_override = OfflineProvider()
    admin = {"Authorization": "Bearer " + (root / "data/admin.token").read_text()}
    bridge = {"Authorization": "Bearer " + (root / "data/bridge.token").read_text()}
    with TestClient(app) as client:
        yield client, root, app.state.engine, admin, bridge
    app.state.engine.memory.close()


def simulate(client, headers, text, **fields):
    return client.post("/admin/simulate", headers=headers,
                       json={"kind": "group", "scope_id": "g1", "user_id": "u1", "text": text, **fields})


def test_authentication_separates_admin_and_bridge(http):
    client, _, _, admin, bridge = http
    assert client.get("/health").json()["ok"] is True
    assert client.get("/admin/status").status_code == 401
    assert client.get("/admin/status", headers=bridge).status_code == 401
    assert client.post("/bridge/message", headers=admin, json={}).status_code == 401
    assert client.get("/admin/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/admin/status", headers={b"authorization": b"Bearer \xff"}).status_code == 401
    status = client.get("/admin/status", headers=admin)
    assert status.status_code == 200
    assert status.json()["credentials"] == {"deepseek": True, "fish": True, "voice_reference": True, "qq": True}
    assert "test-only-private" not in status.text
    event = {"app_id": "testapp", "kind": "group", "scope_id": "g1", "user_id": "u1", "event_id": "test", "text": "hello"}
    disabled = client.post("/bridge/message", headers=bridge, json=event)
    assert disabled.status_code == 200 and disabled.json()["mode"] == "none"


def test_origin_host_and_response_security_headers(http):
    client, _, _, admin, _ = http
    assert client.get("/admin/settings", headers={**admin, "Origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/admin/settings", headers={**admin, "Host": "untrusted.example"}).status_code == 400
    allowed = client.get("/admin/settings", headers={**admin, "Origin": "http://localhost:6196"})
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    assert allowed.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in allowed.headers["content-security-policy"]


def test_invalid_requests_never_echo_submitted_secrets(http):
    client, _, _, admin, bridge = http
    sentinel = "TEST_ONLY_DO_NOT_ECHO_SECRET"
    wrong_event = {"app_id": "app", "kind": "invalid", "scope_id": "g1", "user_id": "u1", "event_id": "event", "text": sentinel}
    responses = [
        client.post("/bridge/message", headers=bridge, json=wrong_event),
        client.put("/admin/settings", headers=admin, json={"api_key": sentinel}),
        client.post("/admin/simulate", headers=admin, json=[sentinel]),
        client.put("/admin/persona", headers=admin, json=[sentinel]),
        client.post("/bridge/ack", headers=bridge, json=[sentinel]),
        client.post("/admin/simulate", headers={**admin, "Content-Type": "application/json"}, content=sentinel),
        client.post("/admin/simulate", headers=admin, json=wrong_event),
    ]
    for response in responses:
        assert response.status_code in (400, 422)
        assert sentinel not in response.text
    oversized = client.put("/admin/persona", headers=admin, json={"text": "x" * 250001})
    assert oversized.status_code == 413


def test_settings_require_reachable_owner_whitelist_and_voice_authorization(http):
    client, root, _, admin, _ = http
    for candidate in (
        Settings(qq_enabled=True),
        Settings(qq_enabled=True, app_id="app", owners=[Owner(kind="group", scope_id="g1", user_id="owner")]),
        Settings(qq_enabled=True, app_id="app", allowed_groups=["g1"]),
        Settings(qq_enabled=True, app_id="app", allowed_groups=["g1"], owners=[Owner(kind="group", scope_id="g2", user_id="owner")]),
        Settings(qq_enabled=True, app_id="app", allowed_dm_users=["owner"], owners=[Owner(kind="dm", scope_id="wrong", user_id="owner")]),
        Settings(voice_enabled=True, voice_authorized=False),
    ):
        assert client.put("/admin/settings", headers=admin, json=candidate.model_dump()).status_code == 400
    valid = Settings(app_id="app", qq_enabled=True, allowed_groups=["g1"],
                     owners=[Owner(kind="group", scope_id="g1", user_id="owner")],
                     voice_enabled=True, voice_authorized=True)
    assert client.put("/admin/settings", headers=admin, json=valid.model_dump()).status_code == 200
    assert read_settings(root).qq_enabled
    assert (root / "config/local.json").stat().st_mode & 0o777 == 0o600


def test_demo_memory_and_history_crud_and_forget(http):
    client, _, engine, admin, _ = http
    assert simulate(client, admin, "/记忆 开启").status_code == 200
    assert simulate(client, admin, "喜欢周末玩游戏").status_code == 200
    history = client.get("/admin/history", headers=admin, params={"scope": DEMO, "user": "u1"}).json()["history"]
    assert [row["role"] for row in history] == ["user", "assistant"]
    user_history_id = history[0]["id"]
    inferred = engine.memory.add_memory(DEMO, "u1", "旧的摘要", source="inferred", source_ids=[user_history_id])
    correction = client.patch(f"/admin/history/{user_history_id}", headers=admin,
                              json={"scope": DEMO, "user": "u1", "text": "喜欢周末散步"})
    assert correction.status_code == 200
    assert all(m["id"] != inferred for m in engine.memory.list_memories(DEMO, "u1"))
    assert len(engine.memory.recent_history(DEMO, "u1")) == 1
    created = client.post("/admin/memories", headers=admin,
                          json={"scope": DEMO, "user": "u1", "text": "喜欢散步"})
    number = created.json()["id"]
    assert client.patch(f"/admin/memories/{number}", headers=admin,
                        json={"scope": DEMO, "user": "u1", "text": "喜欢跑步"}).status_code == 200
    rows = client.get("/admin/memories", headers=admin, params={"scope": DEMO, "user": "u1"}).json()["memories"]
    assert rows[0]["text"] == "喜欢跑步"
    # Even the administrator cannot accidentally mutate a different scope via an ID.
    wrong_scope = client.delete(f"/admin/memories/{number}", headers=admin, params={"scope": "qq:app:group:g1", "user": "u1"})
    assert wrong_scope.status_code == 403
    assert client.delete(f"/admin/memories/{number}", headers=admin, params={"scope": DEMO, "user": "u1"}).status_code == 200
    assert client.delete(f"/admin/history/{user_history_id}", headers=admin, params={"scope": DEMO, "user": "u1"}).status_code == 200
    assert simulate(client, admin, "/记住 临时偏好").status_code == 200
    result = client.post("/admin/forget", headers=admin, json={"scope": DEMO, "user": "u1"})
    assert result.status_code == 200 and "旧备份" in result.json()["backup_note"]
    assert engine.memory.list_memories(DEMO, "u1") == []
    assert engine.memory.recent_history(DEMO, "u1") == []
    assert not engine.memory.consent(DEMO, "u1")


def test_media_endpoint_blocks_traversal_and_symlink_escape(http):
    client, root, _, admin, bridge = http
    media = root / "data/media"
    media.mkdir(exist_ok=True)
    safe_name = "a" * 32 + ".wav"
    (media / safe_name).write_bytes(b"test-audio")
    secret_file = root / "private-test-file"
    secret_file.write_text("test-only-private-file")
    link_name = "b" * 32 + ".wav"
    (media / link_name).symlink_to(secret_file)
    assert client.get("/admin/media/" + safe_name, headers=admin).content == b"test-audio"
    assert client.get("/admin/media/" + safe_name, headers=bridge).status_code == 401
    for name in (link_name, "../admin.token", "%2e%2e%2fadmin.token", "admin.token", "a.wav"):
        response = client.get("/admin/media/" + name, headers=admin)
        assert response.status_code == 404
        assert "test-only-private-file" not in response.text


def test_backup_excludes_credentials_tokens_and_restores_in_independent_root(http, tmp_path):
    client, root, engine, admin, _ = http
    simulate(client, admin, "/记忆 开启")
    simulate(client, admin, "/记住 可恢复的偏好")
    simulate(client, admin, "可恢复的聊天")
    secret_marker = "TEST_ONLY_PRIVATE_BACKUP_EXCLUSION"
    (root / ".env").write_text("DEEPSEEK_API_KEY=" + secret_marker)
    (root / "data/credentials.json").write_text(json.dumps({"key": secret_marker}))
    token_values = [(root / "data/admin.token").read_bytes(), (root / "data/bridge.token").read_bytes()]
    engine.groups.add(DEMO,"u1","临时群正文不应备份","group1",display_name="测试成员")
    engine.groups.set_optout(DEMO,"u2",True)
    response = client.post("/admin/backup", headers=admin)
    assert response.status_code == 200
    backup = Path(response.json()["file"])
    assert backup.is_relative_to(root / "data/backups")
    assert json.loads((backup / "manifest.json").read_text())["contains_credentials"] is False
    assert {p.name for p in backup.iterdir()} == {"memory.sqlite3", "state.sqlite3", "group_state.sqlite3", "bilibili.sqlite3", "content", "settings.json", "profiles.json", "manifest.json"}
    assert "临时群正文不应备份".encode() not in (backup/"group_state.sqlite3").read_bytes()
    for path in backup.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert secret_marker.encode() not in raw
            assert all(value not in raw for value in token_values)
            assert path.stat().st_mode & 0o777 == 0o600
    # Restore snapshots only into an independent root; keep QQ disabled until
    # the operator explicitly completes the documented single-consumer cutover.
    restored_root = tmp_path / "restored"
    (restored_root / "data").mkdir(parents=True)
    shutil.copy2(backup / "memory.sqlite3", restored_root / "data/memory.sqlite3")
    shutil.copy2(backup / "state.sqlite3", restored_root / "data/state.sqlite3")
    shutil.copy2(backup / "group_state.sqlite3", restored_root / "data/group_state.sqlite3")
    shutil.copytree(backup / "content", restored_root / "content")
    restored_settings = Settings.model_validate_json((backup / "settings.json").read_text())
    restored_settings.qq_enabled = False
    write_settings(restored_root, restored_settings)
    restored = Engine(restored_root)
    try:
        assert restored.memory.list_memories(DEMO, "u1")[0]["text"] == "可恢复的偏好"
        assert len(restored.memory.recent_history(DEMO, "u1")) == 2
        assert restored.memory.consent(DEMO, "u1")
        assert len(restored.ledger.deliveries()) == 3
        assert not read_settings(restored_root).qq_enabled
        assert restored.groups.familiarity(DEMO,"u1")["message_count"]==1
        assert restored.groups.opted_out(DEMO,"u2")
        assert restored.groups.snapshot(DEMO)==[]
        assert (restored_root / "content/persona.md").read_bytes() == (root / "content/persona.md").read_bytes()
        engine.memory.forget_me(DEMO, "u1")
        assert restored.memory.list_memories(DEMO, "u1")
    finally:
        restored.memory.close()


def test_concurrent_admin_file_writes_remain_atomic(tmp_path):
    target = tmp_path / "settings.json"
    values = [json.dumps({"sequence": n, "text": str(n) * 1000}) for n in range(16)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda text: private_write(target, text), values))
    assert target.read_text() in values
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


def test_group_management_exposes_metadata_only_and_clear_preserves_limits(http):
    client,_,engine,admin,bridge=http
    scope="onebot:10001:group:30001"
    engine.groups.add(scope,"20001","临时正文不应显示在管理统计","1",display_name="测试群友")
    engine.groups.reserve(scope,cooldown_seconds=90,hour_limit=8,day_limit=40)
    for headers in ({},bridge):
        assert client.get('/admin/groups',params={'scope':scope},headers=headers).status_code==401
        assert client.post('/admin/groups/clear',json={'scope':scope},headers=headers).status_code==401
    result=client.get('/admin/groups',params={'scope':scope},headers=admin)
    assert result.status_code==200 and '临时正文不应显示' not in result.text
    assert result.json()['members'][0]['user_id']=='20001'
    assert result.json()['budget']['hour_used']==1
    assert client.post('/admin/groups/clear',json={'scope':scope},headers=admin).status_code==200
    assert engine.groups.snapshot(scope)==[]
    assert engine.groups.familiarity(scope,'20001')['message_count']==1
    assert engine.groups.budget_status(scope)['hour_used']==1


def test_group_ack_only_bridge_can_confirm_real_receipt(http):
    import time
    client,_,engine,admin,bridge=http
    reply='a'*32;scope='onebot:10001:group:30001'
    engine._group_pending[reply]=dict(scope=scope,user_id='20001',self_id='10001',text='已实际发送的群回复',reservation=None,created_at=time.monotonic())
    body={'reply_id':reply,'status':'sent','message_id':'12345'}
    assert client.post('/bridge/ack',json=body,headers=admin).status_code==401
    assert client.post('/bridge/ack',json={**body,'message_id':'arbitrary'},headers=bridge).status_code==400
    assert engine.groups.snapshot(scope)==[]
    assert client.post('/bridge/ack',json=body,headers=bridge).status_code==200
    assert engine.groups.snapshot(scope)[0]['message_id']=='12345'
    assert not engine.groups.budget_status(scope)['allowed']
