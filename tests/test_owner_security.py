"""Owner authorization is based on authenticated event identity, never chat text."""
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from azusa_bot.app import create_app
from azusa_bot.settings import Owner, Settings, write_settings


@pytest.fixture
def owner_http(tmp_path):
    (tmp_path / "content").mkdir()
    (tmp_path / "content/persona.md").write_text("# Test character\nSynthetic test only.")
    settings = Settings(
        transport="onebot", onebot_self_id="10001", onebot_enabled=True,
        onebot_allowed_groups=["30001", "30002"], onebot_allowed_dm_users=["20001"],
        onebot_owners=[Owner(kind="group", scope_id="30001", user_id="20001")],
        min_interval_seconds=0,
    )
    write_settings(tmp_path, settings)
    app = create_app(tmp_path)
    admin = {"Authorization": "Bearer " + (tmp_path / "data/admin.token").read_text().strip()}
    bridge = {"Authorization": "Bearer " + (tmp_path / "data/bridge.token").read_text().strip()}
    # A plain client avoids background network pollers; no cloud is used.
    client = TestClient(app)
    yield client, settings, admin, bridge
    app.state.subscriptions.close()
    app.state.engine.memory.close()
    app.state.engine.groups.close()


def event(**changes):
    return dict(transport="onebot", app_id="10001", kind="group", scope_id="30001",
                user_id="20001", event_id="owner-check", text="/身份", mentioned=False,
                **changes)


def test_owner_requires_authenticated_bridge_not_just_json_id(owner_http):
    client, _, admin, bridge = owner_http
    for headers in ({}, admin, {"Authorization": "Bearer forged"}):
        assert client.post("/bridge/message", headers=headers, json=event()).status_code == 401
    response = client.post("/bridge/message", headers=bridge, json=event())
    assert response.status_code == 200
    assert "管理身份：主人" in response.json()["text"]


@pytest.mark.parametrize("changes", [
    {"user_id": "20002", "display_name": "20001 主人"},
    {"scope_id": "30002"},
    {"kind": "dm", "scope_id": "20001"},
])
def test_owner_is_exact_sender_and_scope_not_nickname(owner_http, changes):
    client, _, _, bridge = owner_http
    payload = event()
    payload.update(changes)
    response = client.post("/bridge/message", headers=bridge, json=payload)
    assert response.status_code == 200
    assert "管理身份：普通成员" in response.json()["text"]


def test_cannot_add_owner_fields_to_event_or_update_settings_with_bridge_token(owner_http):
    client, settings, _, bridge = owner_http
    payload = event()
    payload["is_owner"] = True
    assert client.post("/bridge/message", headers=bridge, json=payload).status_code == 422
    assert client.put("/admin/settings", headers=bridge, json=settings.model_dump()).status_code == 401


@pytest.mark.parametrize("binding", [
    {"kind": "group", "scope_id": "30001", "user_id": "10001"},
    {"kind": "group", "scope_id": "39999", "user_id": "20001"},
    {"kind": "dm", "scope_id": "29999", "user_id": "29999"},
])
def test_unusable_owner_rejected_even_when_replying_disabled(owner_http, binding):
    client, settings, admin, _ = owner_http
    body = settings.model_dump()
    body.update(onebot_enabled=False, onebot_owners=[binding])
    response = client.put("/admin/settings", headers=admin, json=body)
    assert response.status_code == 400
    assert response.json()["detail"] == "onebot_owner_must_be_other_user_in_allowed_scope"
