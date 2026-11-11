"""Personal QQ/OneBot boundaries stay separate from official bot OpenIDs."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from support_content import seed_content

from azusa_bot.app import create_app
from azusa_bot.core import Engine, Incoming
from azusa_bot.secrets import Credentials
from azusa_bot.settings import Owner, Settings, read_settings, write_settings
from azusa_bot.providers import mock_group_plan
from context_helpers import context as wire_context


SCOPE = "onebot:10001:group:30001"


class OfflineProvider:
    def __init__(self):
        self.calls = []
        self.plan_calls = []

    async def complete(self, messages, *, summary=False):
        if not summary and wire_context(messages).get('task') == 'group_plan':
            self.plan_calls.append(messages)
            return mock_group_plan(wire_context(messages))
        self.calls.append(messages)
        return {"memories": []} if summary else {"text": "收到你的消息。", "mode": "text"}


@pytest.fixture
def onebot(tmp_path):
    seed_content(tmp_path)
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
                        onebot_allowed_groups=["30001"], onebot_allowed_dm_users=["20001"],
                        min_interval_seconds=0)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    provider = OfflineProvider()
    engine.provider_override = provider
    yield engine, settings, provider
    engine.memory.close()


def event(text="你好", **changes):
    values = {"transport": "onebot", "app_id": "10001", "kind": "group", "scope_id": "30001",
              "user_id": "20001", "event_id": "event1", "text": text}
    values.update(changes)
    return Incoming(**values)


def test_existing_official_config_and_event_default_remain_compatible():
    settings = Settings(app_id="official_app", owners=[Owner(kind="dm", scope_id="open_id", user_id="open_id")])
    incoming = Incoming(app_id="official_app", kind="dm", scope_id="open_id", user_id="open_id", event_id="event", text="test")
    assert settings.transport == incoming.transport == "official"
    assert not settings.onebot_enabled and settings.onebot_self_id == ""
    assert settings.onebot_owners == []
    assert settings.is_owner("dm", "open_id", "open_id")
    assert not settings.is_owner("dm", "open_id", "open_id", "onebot")


def test_onebot_configuration_and_events_require_canonical_numeric_ids():
    for invalid in ("OpenID_abc", "-123", "0", "00123", "123 456", "1" * 21):
        for field in ("onebot_self_id", "onebot_allowed_groups", "onebot_allowed_dm_users"):
            with pytest.raises(ValidationError):
                Settings(**{field: [invalid] if field.endswith(("groups", "users")) else invalid})
        for field in ("app_id", "scope_id", "user_id"):
            with pytest.raises(ValidationError):
                event(**{field: invalid})
        with pytest.raises(ValidationError):
            Settings(onebot_owners=[Owner(kind="group", scope_id="30001", user_id=invalid)])
    with pytest.raises(ValidationError):
        Settings(onebot_owners=[Owner(kind="dm", scope_id="20001", user_id="20002")])


@pytest.mark.asyncio
async def test_wrong_transport_self_account_and_scope_are_rejected_before_model(onebot):
    engine, settings, provider = onebot
    cases = [
        (event(transport="official"), "transport_not_selected"),
        (event(app_id="10002"), "onebot_disabled_or_wrong_self"),
        (event(user_id="10001"), "self_event"),
        (event(scope_id="30002"), "group_not_allowed"),
        (event(kind="dm", scope_id="20002", user_id="20002"), "dm_not_allowed"),
        (event(kind="dm", scope_id="20002", user_id="20001"), "dm_not_allowed"),
    ]
    for incoming, reason in cases:
        result = await engine.handle(incoming)
        assert result["mode"] == "none" and result["reason"] == reason
    settings.onebot_enabled = False
    write_settings(engine.root, settings)
    assert (await engine.handle(event()))["reason"] == "onebot_disabled_or_wrong_self"
    assert provider.calls == []
    assert engine.ledger.deliveries() == []
    assert engine.memory.stats()["history"] == 0


@pytest.mark.asyncio
async def test_official_owner_does_not_grant_onebot_permissions(onebot):
    engine, settings, provider = onebot
    settings.owners = [Owner(kind="group", scope_id="30001", user_id="20001")]
    write_settings(engine.root, settings)
    identity = await engine.handle(event("/身份"))
    assert "管理身份：普通成员" in identity["text"]
    denied = await engine.handle(event("/群记住 不该保存", event_id="deny"))
    assert "操作未完成" in denied["text"]
    assert engine.memory.list_memories(SCOPE, "20001") == []
    settings.onebot_owners = [Owner(kind="group", scope_id="30001", user_id="20001")]
    write_settings(engine.root, settings)
    allowed = await engine.handle(event("/群记住 正确的群规则", event_id="allow"))
    assert "已保存" in allowed["text"]
    assert engine.memory.list_memories(SCOPE, "20001")[0]["text"] == "正确的群规则"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_onebot_prompt_and_history_do_not_leak_from_other_transport_or_account(onebot):
    engine, _, provider = onebot
    fixtures = [
        (SCOPE, "当前成员喜欢游戏"),
        ("qq:10001:group:30001", "官方通道秘密喜欢游戏"),
        ("onebot:10002:group:30001", "其他登录账号秘密喜欢游戏"),
        ("onebot:10001:dm:20001", "私聊秘密喜欢游戏"),
    ]
    for scope, text in fixtures:
        engine.memory.add_memory(scope, "20001", text)
        engine.memory.set_consent(scope, "20001", True)
        engine.memory.add_history(scope, "20001", "user", text, "seed")
    response = await engine.handle(event("喜欢游戏"))
    assert response["scope"] == SCOPE
    prompt = json.dumps(provider.calls[0], ensure_ascii=False)
    assert "当前成员喜欢游戏" in prompt
    for private in ("官方通道秘密", "其他登录账号秘密", "私聊秘密"):
        assert private not in prompt
    assert len(engine.memory.recent_history(SCOPE, "20001")) == 2
    engine.acknowledge(response["reply_id"], "sent", "90001")
    assert len(engine.memory.recent_history(SCOPE, "20001")) == 3


@pytest.mark.asyncio
async def test_onebot_private_chat_works_without_official_bot_credentials(onebot):
    engine, settings, provider = onebot
    assert settings.app_id == "" and settings.qq_enabled is False
    reply = await engine.handle(event(kind="dm", scope_id="20001"))
    assert reply["mode"] == "text" and reply["scope"] == "onebot:10001:dm:20001"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_onebot_demo_is_separate_from_live_and_official_demo(onebot):
    engine, _, _ = onebot
    demo = await engine.handle(event("/记住 个人号演示偏好"), demo=True)
    assert demo["scope"] == "demo:onebot:10001:group:30001"
    real = await engine.handle(event("/记忆"))
    assert "个人号演示偏好" not in real["text"]
    old_demo = await engine.handle(event("/记忆", transport="official"), demo=True)
    assert old_demo["scope"] == "demo:group:30001"
    assert "个人号演示偏好" not in old_demo["text"]


def test_onebot_http_enable_requires_self_and_whitelist_but_chat_admin_is_optional(tmp_path, monkeypatch):
    seed_content(tmp_path)
    monkeypatch.setattr("azusa_bot.app.load_credentials", lambda _: Credentials())
    app = create_app(tmp_path)
    app.state.engine.provider_override = OfflineProvider()
    admin = {"Authorization": "Bearer " + (tmp_path / "data/admin.token").read_text()}
    try:
        with TestClient(app) as client:
            no_self = Settings(transport="onebot", onebot_enabled=True, onebot_allowed_dm_users=["20001"])
            no_target = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001")
            for bad in (no_self, no_target):
                assert client.put("/admin/settings", headers=admin, json=bad.model_dump()).status_code == 400
            valid = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001", onebot_allowed_dm_users=["20001"])
            assert client.put("/admin/settings", headers=admin, json=valid.model_dump()).status_code == 200
            status = client.get("/admin/status", headers=admin).json()
            assert status["transport"] == "onebot" and status["transport_enabled"]
            assert status["official_qq_credentials_required"] is False
            assert status["qq_connected"] is False
            assert status["credentials"]["qq"] is False
            assert read_settings(tmp_path).onebot_owners == []
            # Every optional chat-admin must be reachable and not be self.
            for owner in (Owner(kind="dm", scope_id="10001", user_id="10001"),
                          Owner(kind="group", scope_id="30002", user_id="20001")):
                invalid = valid.model_copy(update={"onebot_owners": [owner]})
                assert client.put("/admin/settings", headers=admin, json=invalid.model_dump()).status_code == 400
            delegated = valid.model_copy(update={"onebot_owners": [Owner(kind="dm", scope_id="20001", user_id="20001")]})
            assert client.put("/admin/settings", headers=admin, json=delegated.model_dump()).status_code == 200
            both = delegated.model_copy(update={"qq_enabled": True})
            assert client.put("/admin/settings", headers=admin, json=both.model_dump()).status_code == 400
            wrong_selection = delegated.model_copy(update={"transport": "official"})
            assert client.put("/admin/settings", headers=admin, json=wrong_selection.model_dump()).status_code == 400
            # Demo uses the selected transport/self ID but never writes real scope.
            demo = client.post("/admin/simulate", headers=admin,
                               json={"kind": "dm", "scope_id": "20001", "user_id": "20001", "text": "/记住 演示内容"})
            assert demo.status_code == 200 and demo.json()["scope"] == "demo:onebot:10001:dm:20001"
            assert app.state.engine.memory.list_memories("onebot:10001:dm:20001", "20001") == []
    finally:
        app.state.engine.memory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('participation',[False,True])
@pytest.mark.parametrize('legacy_activity_only',[False,True])
async def test_native_self_at_without_words_calls_role_and_preserves_empty_input(onebot,participation,legacy_activity_only):
    from context_helpers import context
    engine,settings,provider=onebot
    settings.group_chat_enabled=participation
    settings.group_chat_debounce_seconds=0
    write_settings(engine.root,settings)
    engine.memory.set_consent(SCOPE,'20001',True)
    message=event(text='  ',mentioned_user_ids=['10001'],activity_only=legacy_activity_only)
    reply=await engine.handle(message)
    assert reply['mode']=='text' and len(provider.calls)==1,reply
    current=context(provider.calls[0])['current_message']
    assert current['text']=='' and current['mentioned'] is True and current['mention_only'] is True
    assert current['sender']['user_id']=='20001' and current['bot_user_id']=='10001'
    assert '没有附加文字' in provider.calls[0][0]['content']
    assert engine.memory.recent_history(SCOPE,'20001')==[]
    trace=engine.trajectories.list()['items'][0]
    assert trace['request_kind']=='mention' and not trace['request_tail'] and trace['provider_called']
    engine.acknowledge(reply['reply_id'],'sent','-8801')
    dialogue=engine._dialogue_context(message,SCOPE,settings)
    assert dialogue[0]['mention_only'] is True and not dialogue[0]['text'].strip()
    assert dialogue[1]['text']==reply['text']
    assert (await engine.handle(message))['reason']=='duplicate_event'
    assert len(provider.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('ids,everyone',[([],False),(['20002'],False),([],True)])
async def test_nonself_empty_activity_does_not_become_direct_call(onebot,ids,everyone):
    engine,_,provider=onebot
    reply=await engine.handle(event(text='',activity_only=True,mentioned=True,
                                   mentioned_user_ids=ids,mentions_everyone=everyone))
    assert reply['reason']=='activity_observed' and not provider.calls


@pytest.mark.asyncio
async def test_only_at_does_not_override_privacy_whitelist_or_limits(onebot):
    engine,settings,provider=onebot
    hidden=await engine.handle(event(text='（（别看这条',mentioned_user_ids=['10001']))
    assert hidden['reason']=='message_hidden' and not engine.ledger.deliveries()
    outside=await engine.handle(event(text='',scope_id='30002',mentioned_user_ids=['10001']))
    assert outside['reason']=='group_not_allowed' and not provider.calls
    settings.min_interval_seconds=60;write_settings(engine.root,settings)
    first=await engine.handle(event(text='',mentioned_user_ids=['10001']))
    assert first['mode']=='text'
    again=await engine.handle(event(text='',event_id='event2',mentioned_user_ids=['10001']))
    assert again['mode']=='none' and len(provider.calls)==1
