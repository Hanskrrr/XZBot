"""Image and addressing controls use synthetic local profiles only."""
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from azusa_bot.app import create_app
from azusa_bot.group_addressing import address_names, is_addressed
from azusa_bot.profiles import ProfileError, ProfileStore, ProfileEngineManager
from azusa_bot.settings import Settings, private_write, read_settings, write_settings


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "bot"
    private_write(root / "content/persona.md", "# 合成角色\n这是离线配置测试使用的原创人物。")
    write_settings(root, Settings(fish_reference_id="fixture-voice", fish_model_override=True))
    return root


def test_legacy_profile_names_are_empty_without_rewriting_manifest(root):
    store = ProfileStore(root)
    value = json.loads(store.path.read_text())
    value["profiles"][0].pop("address_names")
    private_write(store.path, json.dumps(value))
    original = store.path.read_bytes()
    reopened = ProfileStore(root)
    assert reopened.get("default")["address_names"] == []
    assert reopened.list()["profiles"][0]["address_names"] == []
    assert store.path.read_bytes() == original


def test_aliases_are_profile_scoped_editable_and_do_not_change_owner_or_voice(root):
    store = ProfileStore(root)
    voice = store.voice("default")
    store.update("default", {"address_names": [" 小梓 ", "Azusa"]})
    second = store.create({"name": "第二角色", "address_names": ["小序", "XiaoXu"]})
    assert store.get("default")["address_names"] == ["小梓", "Azusa"]
    assert second["address_names"] == ["小序", "XiaoXu"]
    store.update(second["id"], {"name": "改名"})
    assert store.get(second["id"])["address_names"] == ["小序", "XiaoXu"]
    store.update(second["id"], {"address_names": []})
    assert store.get(second["id"])["address_names"] == []
    assert store.voice("default") == voice
    assert read_settings(root).onebot_owners == []


@pytest.mark.parametrize("invalid", [
    None, "小梓", ("小梓",), {}, [None], [True], [""], [" "], ["梓"],
    ["!！"], ["👋👋"], ["\t小梓"], ["小\n梓"], ["小\x00梓"], ["小\u200b梓"],
    ["x" * 31], ["小梓", " 小梓 "], ["Azusa", "AZUSA"], ["café", "cafe\u0301"],
    [f"角色{i}" for i in range(13)],
])
def test_invalid_aliases_rejected_before_any_profile_write(root, invalid):
    store = ProfileStore(root)
    original = store.path.read_bytes()
    with pytest.raises(ProfileError, match="profile_address_names_invalid"):
        store.update("default", {"name": "不应写入", "address_names": invalid})
    assert store.path.read_bytes() == original
    with pytest.raises(ProfileError, match="profile_address_names_invalid"):
        store.create({"name": "不应创建", "address_names": invalid})
    assert store.path.read_bytes() == original


def test_pinned_engine_reads_names_for_its_profile_after_switch(root):
    manager = ProfileEngineManager(root)
    manager.profiles.update("default", {"address_names": ["小梓"]})
    other = manager.profiles.create({"name": "第二角色", "address_names": ["小序"]})
    try:
        with manager.pin() as first:
            assert first.address_names_loader() == ["小梓"]
            manager.activate(other["id"])
            assert first.address_names_loader() == ["小梓"]
        assert manager.current.address_names_loader() == ["小序"]
        manager.profiles.update(other["id"], {"address_names": ["序序"]})
        assert manager.current.address_names_loader() == ["序序"]
    finally:
        manager.close()


@pytest.mark.parametrize("text,names,expected", [
    ("小梓你觉得这张图怎么样", ["小梓"], True),
    ("大家好，Azusa!", ["Azusa"], True),
    ("AZUSA你好", ["Azusa"], True),
    ("xzbot 的主人是谁", ["XZBot"], True),
    ("Annex 里有幅画", ["Ann"], False),
    ("hello_banner", ["Ann"], False),
    ("XZBot123", ["XZBot"], False),
    ("Anna，你好", ["Ann"], False),
    ("你好", [], False),
    ("梓", ["梓"], False),
    ("小梓", "小梓", False),
    ({"text": "小梓"}, ["小梓"], False),
])
def test_addressing_is_only_a_bounded_text_cue(text, names, expected):
    assert is_addressed(text, names) is expected


@pytest.mark.parametrize("invalid", [0, 1, "true", "false", None, [], {}])
def test_vision_switch_is_strict_boolean(invalid):
    assert Settings().vision_enabled is True
    with pytest.raises(ValidationError):
        Settings(vision_enabled=invalid)


def test_admin_vision_toggle_and_profile_aliases_are_authenticated(root):
    app = create_app(root)
    auth = {"Authorization": "Bearer " + (root / "data/admin.token").read_text()}
    with TestClient(app) as client:
        assert client.put("/admin/providers", json={"settings": {"vision_enabled": False}}).status_code == 401
        before = client.get("/admin/providers", headers=auth).json()["settings"]
        assert before["vision_enabled"] is True
        result = client.put("/admin/providers", headers=auth, json={"settings": {"vision_enabled": False}})
        assert result.status_code == 200
        after = result.json()["settings"]
        assert after == before | {"vision_enabled": False}
        assert read_settings(root).vision_enabled is False
        assert client.patch("/admin/profiles/default", json={"address_names": ["小梓"]}).status_code == 401
        result = client.patch("/admin/profiles/default", headers=auth, json={"address_names": ["小梓"]})
        assert result.status_code == 200 and result.json()["address_names"] == ["小梓"]
        rejected = client.patch("/admin/profiles/default", headers=auth, json={"address_names": ["梓"]})
        assert rejected.status_code == 400
        assert rejected.json() == {"detail": "profile_address_names_invalid"}
        assert client.get("/admin/profiles", headers=auth).json()["profiles"][0]["address_names"] == ["小梓"]
    app.state.engine.close()
