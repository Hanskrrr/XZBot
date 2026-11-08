"""All-member group defaults, individual opt-outs and profile isolation."""
from fastapi.testclient import TestClient
import pytest

from azusa_bot.app import create_app
from azusa_bot.memory import MemoryStore
from azusa_bot.settings import Settings, write_settings

GROUP = "onebot:10001:group:30001"
OTHER = "onebot:10001:group:30002"
USER, SECOND = "20001", "20002"


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    yield store
    store.close()


def add_messages(store, user=USER, count=4, scope=GROUP, start=0):
    return [store.add_history(scope, user, "user", f"合成偏好 {index}：喜欢合作游戏", f"{user}-{index}")
            for index in range(start, start + count)]


def test_group_default_captures_each_member_and_future_members_without_fake_consent(memory):
    assert add_messages(memory, count=1) == [None]
    memory.set_group_collection(GROUP, True)
    first = add_messages(memory)
    second = add_messages(memory, SECOND)
    assert all(first + second)
    assert memory.consent(GROUP, USER) and memory.consent(GROUP, SECOND)
    assert memory.collection_source(GROUP, SECOND) == "group_default"
    assert [row["id"] for row in memory.list_history(GROUP, USER)] == first
    assert [row["id"] for row in memory.list_history(GROUP, SECOND)] == second
    assert memory._db.execute("SELECT count(*) FROM consent").fetchone()[0] == 0
    assert memory.known_members(GROUP) == [USER, SECOND]
    assert memory.due_summaries(4, allowed_scopes=[GROUP]) == [
        {"scope": GROUP, "user": USER, "pending": 4},
        {"scope": GROUP, "user": SECOND, "pending": 4}]
    status = memory.collection_status(4)
    assert status["enabled_groups"] == 1 and status["stats"]["group_inherited_members"] == 2
    assert status["stats"]["consenting_members"] == 2
    assert status["stats"]["explicit_enabled_members"] == 0
    assert all(row["collection_source"] == "group_default" for row in status["members"])


def test_group_default_never_crosses_group_dm_or_profile(memory,tmp_path):
    memory.set_group_collection(GROUP, True)
    assert add_messages(memory, scope=OTHER, count=1) == [None]
    assert add_messages(memory, scope="onebot:10001:dm:20001", count=1) == [None]
    assert add_messages(memory, scope="qq:other:group:30001", count=1) == [None]
    other = MemoryStore(tmp_path / "other-profile.sqlite3")
    try:
        assert not other.group_collection_enabled(GROUP)
        assert add_messages(other, count=1) == [None]
    finally:
        other.close()


@pytest.mark.parametrize("erase", ["disable", "forget"])
def test_individual_optout_survives_group_off_on_and_restart(memory, erase):
    memory.set_group_collection(GROUP, True)
    add_messages(memory)
    memory.add_memory(GROUP, USER, "本人主动要求保存的显式资料")
    if erase == "disable":
        memory.set_consent(GROUP, USER, False)
    else:
        memory.forget_me(GROUP, USER)
    for enabled in [False, True, True]:
        memory.set_group_collection(GROUP, enabled)
    assert not memory.consent(GROUP, USER)
    assert memory.collection_source(GROUP, USER) == "member_disabled"
    assert add_messages(memory, count=1, start=5) == [None]
    assert memory.list_history(GROUP, USER) == []
    assert len(memory.list_memories(GROUP, USER)) == (1 if erase == "disable" else 0)
    assert memory.stats()["excluded_members"] == 1
    restored = MemoryStore(memory.path)
    try:
        assert not restored.consent(GROUP, USER)
        assert restored.consent(GROUP, SECOND)
        assert restored.collection_source(GROUP, USER) == "member_disabled"
    finally:
        restored.close()


def test_group_off_pauses_inherited_only_and_preserves_previous_records(memory):
    memory.set_group_collection(GROUP, True)
    ids = add_messages(memory)
    memory.add_memory(GROUP, USER, "自述合作游戏偏好", source="inferred", source_ids=[ids[0]])
    memory.set_consent(GROUP, SECOND, True)
    add_messages(memory, SECOND)
    memory.set_group_collection(GROUP, False)
    assert add_messages(memory, count=1, start=6) == [None]
    assert add_messages(memory, SECOND, count=1, start=6)[0]
    assert len(memory.list_history(GROUP, USER)) == 4
    assert len(memory.list_memories(GROUP, USER)) == 1
    assert memory.due_summaries(4, allowed_scopes=[GROUP]) == [{"scope": GROUP, "user": SECOND, "pending": 5}]
    assert memory.collection_source(GROUP, SECOND) == "member_enabled"
    memory.set_group_collection(GROUP, True)
    assert memory.consent(GROUP, USER)


def test_group_toggle_invalidates_inflight_summary_without_rewinding_completed_work(memory):
    memory.set_group_collection(GROUP, True)
    add_messages(memory)
    first = memory.begin_summary(GROUP, USER, threshold=4)
    assert memory.finish_summary(GROUP, USER, first["batch"], [])["processed"] == 4
    ids = add_messages(memory, start=4)
    pending = memory.begin_summary(GROUP, USER, threshold=4)
    memory.set_group_collection(GROUP, False)
    memory.set_group_collection(GROUP, True)
    assert memory.finish_summary(GROUP, USER, pending["batch"], [{"text":"过期抽取", "source_ids":[ids[0]]}])["status"] == "superseded"
    next_batch = memory.begin_summary(GROUP, USER, threshold=4)["batch"]
    assert [row["id"] for row in next_batch["history"]] == ids
    assert memory.list_memories(GROUP, USER) == []


def test_inherited_summary_source_references_still_cannot_cross_member(memory):
    memory.set_group_collection(GROUP, True)
    first = add_messages(memory)
    foreign = add_messages(memory, SECOND)
    claim = memory.begin_summary(GROUP, USER, threshold=4)["batch"]
    with pytest.raises(ValueError, match="summary_invalid"):
        memory.finish_summary(GROUP, USER, claim, [{"text":"错误归因", "source_ids":[foreign[0]]}])
    result = memory.finish_summary(GROUP, USER, claim, [{"text":"用户自述合作游戏偏好", "source_ids":[first[0]]}])
    assert result["saved"] == 1
    assert memory.list_memories(GROUP, SECOND) == []


@pytest.mark.parametrize("scope", ["onebot:10001:dm:20001", "demo:onebot:10001:group:30001", "group:30001", "qq:a:group:b:extra"])
def test_store_rejects_non_group_policy(scope,memory):
    with pytest.raises(ValueError): memory.set_group_collection(scope, True)


@pytest.fixture
def local(tmp_path):
    (tmp_path / "content").mkdir()
    (tmp_path / "content/persona.md").write_text("用于合成测试的原创角色，不访问外部接口。")
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id="10001",
        onebot_allowed_groups=["30001"], onebot_allowed_dm_users=[USER], auto_summary_every=4))
    app = create_app(tmp_path)
    admin = {"Authorization": "Bearer " + (tmp_path / "data/admin.token").read_text()}
    with TestClient(app) as client:
        yield client, app.state.engine, admin
    app.state.engine.close()


def test_group_api_requires_admin_strict_bool_and_current_whitelist(local):
    client, engine, admin = local
    body = {"scope":GROUP,"enabled":True}
    assert client.put("/admin/memory/group",json=body).status_code == 401
    assert client.put("/admin/memory/group",headers=admin,json=dict(body,enabled="true")).status_code == 422
    for scope in [OTHER,"onebot:10001:dm:20001","onebot:99999:group:30001","qq:10001:group:30001","demo:"+GROUP]:
        denied = client.put("/admin/memory/group",headers=admin,json=dict(body,scope=scope))
        assert denied.status_code == 400
        assert denied.json()["detail"] == "group_collection_requires_allowed_group"
    assert not engine.memory.group_collection_enabled(GROUP)
    result = client.put("/admin/memory/group",headers=admin,json=body)
    assert result.status_code == 200 and result.json()["enabled"] is True


def test_targets_show_inherited_members_even_without_speaking_context(local):
    client, engine, admin = local
    client.put("/admin/memory/group",headers=admin,json={"scope":GROUP,"enabled":True})
    add_messages(engine.memory, count=1)
    engine.memory.set_consent(GROUP, SECOND, False)
    targets = client.get("/admin/memory/targets",headers=admin).json()
    group = next(row for row in targets["scopes"] if row["kind"] == "group")
    assert group["group_collection_enabled"] is True
    members = {row["user"]:row for row in group["members"]}
    assert members[USER]["enabled"] is True and members[USER]["collection_source"] == "group_default"
    assert members[SECOND]["enabled"] is False and members[SECOND]["collection_source"] == "member_disabled"
    assert next(row for row in targets["scopes"] if row["kind"] == "dm")["group_collection_enabled"] is False


def test_group_policy_isolated_after_global_profile_switch_and_stale_write_rejected(local):
    client, engine, admin = local
    client.put("/admin/memory/group",headers=admin,json={"scope":GROUP,"enabled":True})
    created = client.post("/admin/profiles",headers=admin,json={"name":"另一个合成角色"}).json()
    profile_id = created["id"]
    assert client.post(f"/admin/profiles/{profile_id}/activate",headers=admin,json={}).status_code == 200
    group = next(row for row in client.get("/admin/memory/targets",headers=admin).json()["scopes"] if row["kind"] == "group")
    assert not group["group_collection_enabled"]
    stale = client.put("/admin/memory/group",headers=admin | {"X-XZBot-Profile":"default"},json={"scope":GROUP,"enabled":False})
    assert stale.status_code == 409
    client.post("/admin/profiles/default/activate",headers=admin,json={})
    assert engine.memory.group_collection_enabled(GROUP)


def test_group_policy_survives_database_backup(memory,tmp_path):
    memory.set_group_collection(GROUP, True)
    memory.set_consent(GROUP, USER, False)
    backup=tmp_path / "backup.sqlite3"
    memory.backup(backup)
    copied=MemoryStore(backup)
    try:
        assert copied.group_collection_enabled(GROUP)
        assert not copied.consent(GROUP, USER) and copied.consent(GROUP, SECOND)
    finally:
        copied.close()
