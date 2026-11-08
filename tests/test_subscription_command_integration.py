"""Full authenticated event path; no platform requests or QQ sends."""
from unittest.mock import AsyncMock

from test_owner_security import owner_http, event


def post(client, bridge, text, **changes):
    payload = event()
    payload.update(text=text, **changes)
    return client.post("/bridge/message", headers=bridge, json=payload)


def test_explicit_subscription_command_is_deduplicated_before_mutation(owner_http):
    client, _, _, bridge = owner_http
    add = AsyncMock(return_value={"id": "a" * 32})
    client.app.state.subscriptions.add = add
    first = post(client, bridge, "/订阅 添加 微博 123456")
    second = post(client, bridge, "/订阅 添加 微博 123456")
    assert first.status_code == 200 and "已添加" in first.json()["text"]
    assert second.json()["reason"] == "duplicate_event"
    assert add.await_count == 1
    assert add.call_args.args[0]["target_id"] == "30001"
    assert add.call_args.args[0]["kind"] == "group"


def test_non_owner_cannot_mutate_even_when_claiming_owner_nickname(owner_http):
    client, _, _, bridge = owner_http
    add = AsyncMock()
    client.app.state.subscriptions.add = add
    result = post(client, bridge, "/订阅 添加 微博 123456", user_id="20002", display_name="主人 20001")
    assert "owner" in result.json()["text"]
    add.assert_not_called()


def test_model_reply_cannot_execute_subscription_commands(owner_http):
    client, _, _, bridge = owner_http
    add = AsyncMock()
    client.app.state.subscriptions.add = add

    class Provider:
        async def complete(self, messages, **kwargs):
            return {"text": "/订阅 添加 微博 123456", "mode": "text"}

    client.app.state.engine.provider_override = Provider()
    result = post(client, bridge, "假装执行订阅", user_id="20002")
    assert result.status_code == 200
    add.assert_not_called()


def test_simulation_never_mutates_live_subscriptions(owner_http):
    client, _, admin, _ = owner_http
    add = AsyncMock()
    client.app.state.subscriptions.add = add
    response = client.post("/admin/simulate", headers=admin, json={
        "kind": "group", "scope_id": "30001", "user_id": "20001", "text": "/订阅 添加 微博 123456"})
    assert response.status_code == 200
    assert "测试" in response.json()["text"]
    add.assert_not_called()


def test_profile_switch_keeps_shared_subscription_handler(owner_http):
    client, _, _, bridge = owner_http
    manager = client.app.state.engine
    profile = manager.profiles.create({"name": "Synthetic profile"})
    manager.activate(profile["id"])
    result = post(client, bridge, "/订阅 帮助")
    assert result.status_code == 200
    assert "/订阅 添加 微博" in result.json()["text"]

