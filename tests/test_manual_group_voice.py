"""Direct group voice uses synthetic audio and the real claim/receipt routes."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from azusa_bot.app import create_app
from azusa_bot.settings import Settings, write_settings
from support_content import seed_content
from test_group_idle import GROUP, SCOPE, SELF, activity, rig
from test_group_voice import Provider


VOICE_TEXT = "请听原文。\n（语音已发送。）  早上好！ https://example.com"
REQUEST_ID = "a" * 32
SEND = "/admin/groups/voice/send"
STATUS = f"/admin/groups/voice/requests/{REQUEST_ID}"


def test_manual_voice_admin_request_and_single_bridge_receipt(tmp_path, monkeypatch):
    seed_content(tmp_path)
    write_settings(tmp_path, Settings(transport="onebot", onebot_enabled=True,
        onebot_self_id=SELF, onebot_allowed_groups=[GROUP], provider_mode="cloud",
        voice_enabled=True, voice_authorized=True, fish_reference_id="synthetic", fish_model_override=True))
    app = create_app(tmp_path)
    engine = app.state.engine.current
    provider = Provider(tmp_path)
    engine.provider_override = provider
    connection = app.state.qq_connection
    monkeypatch.setattr(connection, "snapshot", lambda: {"state": "offline"})
    client = TestClient(app)
    admin = {"Authorization": "Bearer " + (tmp_path / "data/admin.token").read_text().strip()}
    bridge = {"Authorization": "Bearer " + (tmp_path / "data/bridge.token").read_text().strip()}
    body = {"scope": SCOPE, "request_id": REQUEST_ID, "text": VOICE_TEXT}

    assert client.post(SEND, headers=bridge, json=body).status_code == 401
    assert client.post(SEND, headers=admin, json=body).status_code == 409
    monkeypatch.setattr(connection, "snapshot", lambda: {"state": "online"})
    assert client.post(SEND, headers=admin, json={**body, "scope": f"onebot:{SELF}:group:99999"}).status_code == 409
    assert client.post(SEND, headers=admin, json=body).json()["status"] == "queued"
    assert client.get(STATUS, headers=bridge).status_code == 401
    claim = "/bridge/idle/manual-claim"
    assert client.post(claim, headers=admin, json={"self_id": SELF}).status_code == 401
    assert client.post(claim, headers=bridge, json={"self_id": "99999"}).json() == {"messages": []}

    item, = client.post(claim, headers=bridge, json={"self_id": SELF}).json()["messages"]
    assert (item["self_id"], item["target_id"], item["mode"]) == (SELF, GROUP, "voice")
    assert item["voice_only"] is True
    assert provider.speech == [VOICE_TEXT] and not provider.calls and not provider.plan_calls
    media = Path(item["media_path"])
    assert media.is_file() and not engine.groups.snapshot(SCOPE)
    credentials = {key: item[key] for key in ("id", "claim_token", "self_id")}
    authorize = "/bridge/idle/authorize"
    assert client.post(authorize, headers=bridge, json=credentials).json() == {"allowed": True}
    assert client.post(authorize, headers=bridge, json=credentials).json() == {"allowed": False}
    assert client.post("/bridge/idle/ack", headers=bridge,
        json={**credentials, "status": "sent", "message_id": "-90001"}).json() == {"acknowledged": True}
    status = client.get(STATUS, headers=admin).json()
    assert status["status"] == "sent" and "text" not in status
    assert not media.exists()
    assert client.post(SEND, headers=admin, json=body).json()["status"] == "sent"
    assert client.post(claim, headers=bridge, json={"self_id": SELF}).json() == {"messages": []}
    assert provider.speech == [VOICE_TEXT]
    assert engine.ledger.cloud_budget(engine.settings().daily_cloud_requests)["used"] == 1


@pytest.fixture
def voice_rig(rig):
    engine, settings, _, _ = rig
    settings.voice_enabled = settings.voice_authorized = True
    settings.fish_reference_id = "synthetic"
    write_settings(engine.root, settings)
    provider = Provider(engine.root)
    engine.provider_override = provider
    return engine, settings, provider


@pytest.mark.parametrize("change", ["human", "peer", "context_cleared", "profile_changed"])
async def test_custom_voice_ignores_activity_but_honors_invalidation(voice_rig, change):
    engine, _, provider = voice_rig
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    engine.idle.queue_manual(SCOPE, REQUEST_ID, voice_text=VOICE_TEXT)
    if change == "human":
        await engine.handle(activity(1))
    elif change == "peer":
        engine.idle.peer_spoke(SCOPE)
    assert engine.idle.manual_status(REQUEST_ID, voice=True)["status"] == "queued"
    claim = asyncio.create_task(engine.idle.claim_manual(SELF))
    await asyncio.wait_for(provider.entered.wait(), 2)
    if change == "human":
        await engine.handle(activity(2))
    elif change == "peer":
        engine.idle.peer_spoke(SCOPE)
    elif change == "context_cleared":
        engine.clear_group_context(SCOPE)
    else:
        engine._active_guard = lambda: False
    provider.release.set()
    messages = await claim
    if change in {"human", "peer"}:
        item, = messages
        if change == "human":
            await engine.handle(activity(3))
        else:
            engine.idle.peer_spoke(SCOPE)
        assert engine.idle.manual_status(REQUEST_ID, voice=True)["status"] == "prepared"
        assert engine.idle.authorize(item["id"], item["claim_token"], SELF)
        engine.idle.peer_spoke(SCOPE)
        assert engine.idle.manual_status(REQUEST_ID, voice=True)["status"] == "authorized"
        assert engine.idle.ack(item["id"], item["claim_token"], SELF, "sent", "-90001")["acknowledged"]
    else:
        assert not messages
        assert all(not path.exists() for path in provider.generated)


@pytest.mark.parametrize("error", ["fish_http_503", "voice_budget_exhausted"])
async def test_custom_voice_failure_never_queues_text(voice_rig, error):
    engine, settings, provider = voice_rig
    if error == "fish_http_503":
        provider.error = error
    else:
        settings.daily_cloud_requests = 1
        write_settings(engine.root, settings)
        assert engine.ledger.consume(1)
    engine.idle.queue_manual(SCOPE, REQUEST_ID, voice_text=VOICE_TEXT)
    assert await engine.idle.claim_manual(SELF) == []
    assert not engine.idle.pending and not engine.groups.snapshot(SCOPE)
    assert provider.speech == ([VOICE_TEXT] if error == "fish_http_503" else [])
    status = engine.idle.manual_status(REQUEST_ID, voice=True)
    assert status["status"] == "failed" and status["error_code"] == error
    assert engine.group_voice.status(SCOPE, settings)["used"] == (1 if provider.speech else 0)


@pytest.mark.parametrize(("changes", "error"), [
    ({"voice_enabled": False}, "voice_disabled"),
    ({"voice_authorized": False}, "voice_not_authorized"),
    ({"provider_mode": "mock"}, "voice_requires_cloud"),
    ({"max_voice_chars": 10}, "voice_text_too_long"),
    ({"group_voice_mode": "off"}, "group_voice_disabled"),
    ({"group_voice_mode": "limited", "group_voice_daily_limit": 1}, "group_voice_limit"),
])
def test_custom_voice_policy_rejects_before_synthesis(voice_rig, changes, error):
    engine, settings, provider = voice_rig
    settings = settings.model_copy(update=changes)
    write_settings(engine.root, settings)
    if error == "group_voice_limit":
        assert engine.group_voice.reserve(SCOPE, settings)
    with pytest.raises(ValueError, match=f"^{error}$"):
        engine.idle.queue_manual(SCOPE, REQUEST_ID, voice_text=VOICE_TEXT)
    assert not provider.speech
