"""Two independently authorized accounts, with fake models and no QQ network."""
import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from azusa_bot.accounts import AccountManager, AccountRegistry, AccountError, import_manager_profile, managed_qq_ids
from azusa_bot.app import create_app, backup_project
from azusa_bot.bot_control import BotPowerController, read_control
from azusa_bot.core import Incoming
from azusa_bot.profiles import ProfileStore, ProfileEngineManager
from azusa_bot.runtime_layout import runtime_layout
from azusa_bot.settings import Settings, Owner, private_write, read_settings, write_settings


@pytest.fixture
def manager(tmp_path):
    root = tmp_path / "QQBot"
    private_write(root / "content/persona.md", "# 合成角色\n这是专门用于独立账号测试的人设。")
    private_write(root / "content/lore/source.md", "共享的角色背景，不含聊天历史。")
    private_write(root / "ui/index.html", "<title>Test</title>")
    write_settings(root, Settings(transport="onebot", onebot_self_id="10001", onebot_enabled=True,
        onebot_allowed_groups=["30001"], onebot_owners=[Owner(kind="group", scope_id="30001", user_id="90001")],
        min_interval_seconds=0, fish_model_override=True, fish_reference_id="test-voice"))
    ProfileStore(root)
    private_write(root / "data/napcat-webui.token", "SYNTHETIC_PRIMARY_LOGIN_TOKEN")
    private_write(root / "data/astrbot-source.path", "/synthetic/installed/AstrBot")
    return AccountManager(root)


def create_child(manager, name="测试账号"):
    entry = manager.create({"name": name, "profile_id": "default"})
    return entry, manager.registry.child_root(entry["id"])


def ready(root, qq):
    write_settings(root, read_settings(root).model_copy(update={"onebot_self_id": qq, "onebot_enabled": True,
        "onebot_allowed_groups": ["30001"], "min_interval_seconds": 0}))
    BotPowerController(root, lambda: None)._write("running")


def incoming(qq, user="90001", event_id="same-event"):
    return Incoming(transport="onebot", app_id=qq, kind="group", scope_id="30001", user_id=user,
        text="这是当前账号自己的合成问题。", event_id=event_id, mentioned=True)


def test_create_copies_role_only_keeps_account_stopped_and_uses_private_distinct_tokens(manager):
    first, a = create_child(manager)
    second, b = create_child(manager)
    assert first["slot"] == 1 and second["slot"] == 2
    assert runtime_layout(a).core_port != runtime_layout(b).core_port
    assert (a / "content/persona.md").read_bytes() == (manager.root / "content/persona.md").read_bytes()
    assert read_settings(a).fish_reference_id == "test-voice"
    assert read_control(a)["state"] == "stopped"
    assert not read_settings(a).onebot_self_id and not read_settings(a).onebot_enabled
    assert read_settings(a).onebot_allowed_groups == [] and read_settings(a).onebot_owners == []
    assert not (a / "data/napcat-webui.token").exists()
    assert not (a / "data/memory.sqlite3").exists() and not (a / "data/ledger.sqlite3").exists()
    assert not (a / "runtime/napcat-prepared.json").exists()
    assert (a / "data/admin.token").read_text() != (b / "data/admin.token").read_text()
    assert (a / "data/bridge.token").read_text() != (b / "data/bridge.token").read_text()
    assert (a / "data/admin.token").stat().st_mode & 0o077 == 0
    assert (a / "data/keychain-service").read_text() == (b / "data/keychain-service").read_text()


async def test_two_accounts_same_event_and_group_do_not_share_memory_or_invalidate_each_other(manager):
    _, a = create_child(manager, "甲")
    _, b = create_child(manager, "乙")
    ready(a, "20001"); ready(b, "20002")
    left, right = ProfileEngineManager(a), ProfileEngineManager(b)
    started, release = asyncio.Event(), asyncio.Event()
    class Slow:
        async def complete(self, *_, **__):
            started.set(); await release.wait(); return {"mode": "text", "text": "乙账号自己的回答。"}
    right.provider_override = Slow()
    try:
        left.memory.set_consent("onebot:20001:group:30001", "90001", True)
        right.memory.set_consent("onebot:20002:group:30001", "90001", True)
        reply_a = await left.handle(incoming("20001"))
        assert reply_a["mode"] == "text"
        pending = asyncio.create_task(right.handle(incoming("20002")))
        await asyncio.wait_for(started.wait(), 2)
        new = left.profiles.create({"name": "甲的另一个角色"})
        left.activate(new["id"])
        BotPowerController(a, left.invalidate_activity)._write("stopped")
        release.set(); reply_b = await pending
        assert reply_b["mode"] == "text" and "乙账号" in reply_b["text"]
        assert right.active_profile_id == "default" and read_control(b)["enabled"]
        assert right.authorize_reply(reply_b["reply_id"])
        assert left.memory.stats()["history"] == 0
        assert right.memory.recent_history("onebot:20001:group:30001", "90001") == []
        assert not right.authorize_reply(reply_a["reply_id"])
    finally:
        release.set(); left.close(); right.close()


async def test_peer_commands_require_local_identity_configuration(manager):
    _, a = create_child(manager)
    _, b = create_child(manager)
    ready(a, "20001"); ready(b, "20002")
    assert managed_qq_ids(a) == {"10001", "20001", "20002"}
    engine = ProfileEngineManager(a)
    try:
        unconfigured = await engine.handle(incoming("20001", "20002", "unconfigured").model_copy(update={"text": "/身份"}))
        assert unconfigured["mode"] == "text" and unconfigured.get("reason") not in {"managed_bot_sender", "peer_bot_sender"}
        write_settings(a, read_settings(a).model_copy(update={"onebot_peer_bot_ids": ["10001", "20002"]}))
        for sender in ("10001", "20002"):
            result = await engine.handle(incoming("20001", sender, "configured-" + sender).model_copy(update={"text": "/记住 不应保存"}))
            assert result["reason"] == "peer_bot_sender"
        assert engine.memory.stats()["memories"] == 0
        assert engine.trajectories.list()["items"] == []
    finally:
        engine.close()


def test_copy_role_into_child_does_not_activate_or_copy_memory(manager):
    _, child = create_child(manager)
    source = ProfileStore(manager.root)
    role = source.create({"name": "第二套人设", "persona": "# 第二套人设\n这套资料应能复制到不同账号。",
        "voice": {"fish_reference_id": "second-voice"}})
    private_write(source.directory(role["id"]) / "data/private-history.json", "NEVER_COPY_HISTORY")
    target = ProfileStore(child)
    result = import_manager_profile(child, target, {"profile_id": role["id"]})
    assert result["copied"] and not result["activated"]
    assert target.list()["active_id"] == "default"
    assert target.voice(result["profile_id"])["fish_reference_id"] == "second-voice"
    assert not (target.directory(result["profile_id"]) / "data").exists()
    assert source.list()["active_id"] == "default"


def test_cross_account_tokens_origins_and_duplicate_qq_binding_rejected(manager):
    _, a = create_child(manager)
    _, b = create_child(manager)
    ready(a, "20001"); ready(b, "20002")
    apps = [create_app(a), create_app(b)]
    try:
        with TestClient(apps[0]) as ca, TestClient(apps[1]) as cb:
            auth_a = {"Authorization": "Bearer " + (a / "data/admin.token").read_text().strip()}
            auth_b = {"Authorization": "Bearer " + (b / "data/admin.token").read_text().strip()}
            bridge_a = {"Authorization": "Bearer " + (a / "data/bridge.token").read_text().strip()}
            assert cb.get("/admin/profiles", headers=auth_a).status_code == 401
            assert ca.get("/admin/profiles", headers=auth_b).status_code == 401
            assert cb.post("/bridge/message", headers=bridge_a, json={}).status_code == 401
            assert cb.get("/admin/profiles", headers=auth_b | {"Origin": runtime_layout(a).core_url}).status_code == 403
            assert cb.get("/admin/profiles", headers=auth_b | {"Origin": runtime_layout(b).core_url}).status_code == 200
            changed = read_settings(b).model_dump() | {"onebot_self_id": "20001"}
            response = cb.put("/admin/settings", headers=auth_b, json=changed)
            assert response.status_code == 409 and response.json()["detail"] == "qq_account_already_managed"
            assert read_settings(b).onebot_self_id == "20002"
            assert cb.post("/admin/accounts", headers=auth_b, json={"name": "禁止嵌套", "profile_id": "default"}).status_code == 403
            for number in ["primary", a.name, b.name]:
                response = cb.post(f"/admin/accounts/{number}/management", headers=auth_b, json={})
                assert response.status_code == 403
                assert response.json() == {"detail": "account_manager_only"}
            assert cb.get("/admin/status", headers=auth_b).json()["account_context"]["id"] == b.name
    finally:
        for app in apps: app.state.engine.close()


def test_exact_trajectory_search_is_local_to_each_account(manager):
    _, left = create_child(manager, '甲')
    _, right = create_child(manager, '乙')
    for root, qq, other in ((left, '20001', '20002'), (right, '20002', '20001')):
        ready(root, qq)
        app = create_app(root)
        admin = {'Authorization':'Bearer ' + (root / 'data/admin.token').read_text().strip()}
        path = '/admin/trajectories'
        scope, other_scope = f'onebot:{qq}:group:30001', f'onebot:{other}:group:30001'
        try:
            with TestClient(app) as client:
                response = asyncio.run(app.state.engine.handle(incoming(qq, event_id='-4242')))
                assert response['mode'] == 'text'
                message, = client.get(path + '/events', headers=admin, params={'message_id':'-4242'}).json()['items']
                assert message['reply_id'] == response['reply_id'] and message['scope'] == scope
                call, = client.get(path, headers=admin, params={'call_id':1}).json()['items']
                assert call['id'] == 1 and call['scope'] == scope
                assert client.get(path, headers=admin, params={'call_id':1, 'scope':other_scope}).json()['items'] == []
        finally:
            app.state.engine.close()


async def test_operations_reject_duplicates_and_never_touch_other_root(manager, monkeypatch):
    entry, child = create_child(manager)
    started, release = asyncio.Event(), asyncio.Event()
    async def run(root, action):
        assert root == child and action == "prepare"
        started.set(); await release.wait()
    monkeypatch.setattr(manager, "_run", run)
    result = await manager.operate(entry["id"], "prepare", {})
    assert result["accepted"]
    await started.wait()
    with pytest.raises(AccountError, match="account_busy"):
        await manager.operate(entry["id"], "stop", {})
    with pytest.raises(AccountError):
        await manager.operate("primary", "stop", {})
    release.set(); await asyncio.gather(*list(manager.tasks.values()))
    assert not manager.tasks and read_settings(manager.root).onebot_enabled


def test_limit_symlinks_and_failed_create_do_not_publish_partial_accounts(manager):
    for _ in range(3): create_child(manager)
    with pytest.raises(AccountError, match="account_limit_reached"): create_child(manager)
    with pytest.raises(AccountError): manager.registry.child_root("../outside")
    first = manager.registry.read()[0]
    path = manager.registry.child_root(first["id"]) / "config/instance.json"
    data = json.loads(path.read_text()); data["slot"] = 3
    private_write(path, json.dumps(data))
    with pytest.raises(AccountError, match="accounts_manifest_invalid"): manager.registry.read()


def test_symlinked_role_content_is_not_followed(manager):
    (manager.root / "content/private-link").symlink_to(manager.root / "data")
    with pytest.raises(AccountError, match="account_role_copy_failed"): create_child(manager)
    assert manager.registry.read() == []
    assert list((manager.root / "accounts").iterdir()) == []


def test_backup_keeps_instance_mapping_but_never_other_accounts_or_login(manager):
    _, child = create_child(manager)
    for root, metadata in ((manager.root, "accounts.json"), (child, "instance.json")):
        engine = ProfileEngineManager(root)
        try:
            backup = backup_project(root, engine)
            assert (backup / metadata).is_file()
            assert (backup / metadata).stat().st_mode & 0o077 == 0
            assert json.loads((backup / "manifest.json").read_text())["account_scope"] == "this_account_only"
            assert not (backup / "accounts").exists()
            assert not (backup / "napcat-webui.token").exists()
            assert not (backup / "admin.token").exists()
        finally:
            engine.close()


async def test_management_navigation_reuses_core_without_starting_qq(manager, monkeypatch):
    entry, child = create_child(manager)
    calls = []
    async def request(root, path, **kwargs):
        calls.append((root, path)); return {"project_path": str(root)}
    async def forbidden(*args):
        pytest.fail("opening an existing console must not run a host helper")
    monkeypatch.setattr(manager, "_request", request)
    monkeypatch.setattr(manager, "_run", forbidden)
    private_write(manager.root / "data/admin.token", "SYNTHETIC_PARENT_ADMIN_TOKEN")
    before = read_control(child)
    result = await manager.management_url(entry["id"])
    assert result["url"] == runtime_layout(child).core_url + "/#token=" + (child / "data/admin.token").read_text().strip()
    assert (manager.root / "data/admin.token").read_text().strip() not in result["url"]
    assert read_control(child) == before and not read_settings(child).onebot_enabled
    assert calls == [(child, "/admin/status")]


async def test_management_navigation_only_starts_missing_management_service(manager, monkeypatch):
    entry, child = create_child(manager)
    calls = []
    async def request(root, path, **kwargs):
        if not calls: raise AccountError("account_service_unavailable")
        return {}
    async def run(root, action):
        calls.append((root, action))
    monkeypatch.setattr(manager, "_request", request)
    monkeypatch.setattr(manager, "_run", run)
    await manager.management_url(entry["id"])
    assert calls == [(child, "ensure-core")] and not manager.tasks
    assert not read_control(child)["enabled"]


async def test_child_navigation_metadata_has_return_address_without_parent_access(manager, monkeypatch):
    entry, child = create_child(manager)
    own = AccountManager(child)
    assert own.context() == {"id": entry["id"], "name": entry["name"], "is_manager": False,
                             "manager_url": runtime_layout(manager.root).core_url}
    for number in ["primary", entry["id"]]:
        with pytest.raises(AccountError, match="account_manager_only"):
            await own.management_url(number)
    calls = []
    async def run(root, action): calls.append((root, action))
    monkeypatch.setattr(own, "_run", run)
    assert (await own.prepare_login())["accepted"]
    await asyncio.gather(*list(own.tasks.values()))
    assert calls == [(child, "prepare")]


async def test_account_cards_preserve_power_error_and_reply_switch(manager, monkeypatch):
    entry, child = create_child(manager)
    BotPowerController(child, lambda: None)._write("error", "power_qq_binding_required")
    async def request(*args, **kwargs): raise AccountError("account_service_unavailable")
    monkeypatch.setattr(manager, "_request", request)
    data = await manager.status()
    assert data["context"]["id"] == "primary"
    assert [r["id"] for r in data["accounts"]] == ["primary", entry["id"]]
    row = data["accounts"][1]
    assert row["power_error_code"] == "power_qq_binding_required"
    assert not row["replies_enabled"] and not row["core_running"]
