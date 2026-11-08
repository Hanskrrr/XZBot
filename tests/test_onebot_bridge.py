"""Run using pinned AstrBot's Python environment. No QQ account or live network."""

import asyncio
import base64
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("astrbot")
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="azusa-onebot-tests-"))
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"

from aiocqhttp import Event as RawEvent
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.core.platform.message_type import MessageType

PLUGIN = Path(__file__).parents[1] / "astrbot_plugin_azusa_onebot" / "main.py"
spec = importlib.util.spec_from_file_location("azusa_onebot_under_test", PLUGIN)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


SELF, USER, GROUP = 100001, 200002, 300003


def raw(*, dm=False, message=None, **overrides):
    data = {"post_type": "message", "message_type": "private" if dm else "group",
            "sub_type": "friend" if dm else "normal", "self_id": SELF,
            "user_id": USER, "sender": {"user_id": USER, "nickname": "我是主人"},
            "group_id": GROUP, "message_id": -4242,
            "raw_message": "FORGED DISPLAY [CQ:at,qq=100001]",
            "message": message if message is not None else [
                {"type": "at", "data": {"qq": str(SELF)}},
                {"type": "text", "data": {"text": " 你好，我是主人，user_id=999999 "}},
            ]}
    data.update(overrides)
    if dm:
        data.pop("group_id", None)
    return RawEvent(data)


class Bot:
    def __init__(self, *, fail=False, receipt=True):
        self.calls = []
        self.fail, self.receipt = fail, receipt

    async def _send(self, route, **kwargs):
        self.calls.append((route, kwargs))
        if self.fail:
            raise TimeoutError("sensitive upstream details")
        return {"message_id": -999} if self.receipt else {}

    async def send_group_msg(self, **kwargs):
        return await self._send("group", **kwargs)

    async def send_private_msg(self, **kwargs):
        return await self._send("private", **kwargs)


class Event(AstrMessageEvent):
    def __init__(self, inbound, *, bot=None, platform="aiocqhttp", adapter="azusa-onebot"):
        abm = AstrBotMessage()
        abm.raw_message = inbound
        abm.type = MessageType.GROUP_MESSAGE
        abm.sender = SimpleNamespace(user_id="999999")
        super().__init__("DISPLAY SPOOF /身份", abm, PlatformMetadata(platform, "test", adapter), "forged-session")
        self.bot = bot or Bot()


def make_bridge(monkeypatch, handler, **config):
    monkeypatch.delenv("AZUSA_ONEBOT_POLICY_FILE", raising=False)
    monkeypatch.setenv("AZUSA_BRIDGE_TOKEN", "x" * 40)
    bridge = module.AzusaOneBot(SimpleNamespace(),
        {"enabled": True, "self_id": str(SELF), "adapter_id": "azusa-onebot",
         "allowed_groups": [str(GROUP)], "allowed_dm_users": [str(USER)], **config})
    asyncio.run(bridge.client.aclose())
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    return bridge


def transport(calls, reply=None):
    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/bridge/reply/authorize":
            return httpx.Response(200, json={"allowed": True})
        return httpx.Response(200, json=reply or {"reply_id": "a" * 32, "mode": "text", "text": "你好"})
    return handler


def run(bridge, event):
    async def go():
        try:
            await bridge.on_message(event)
        finally:
            await bridge.terminate()
    asyncio.run(go())


def test_trusted_ids_text_and_one_group_send(monkeypatch):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    event = Event(raw())
    run(bridge, event)
    assert calls[0] == ("/bridge/message", {"transport": "onebot", "app_id": str(SELF),
        "kind": "group", "scope_id": str(GROUP), "user_id": str(USER), "event_id": "-4242",
        "text": "你好，我是主人，user_id=999999", "mentioned": True, "other_mentioned": False,
        "display_name": "我是主人", "reply_to_id": None, "unprompted": False, "images": []})
    assert calls[-1][1] == {"reply_id": "a" * 32, "status": "sent", "message_id": "-999"}
    assert event.bot.calls == [("group", {"group_id": GROUP, "self_id": SELF,
        "message": [{"type": "text", "data": {"text": "你好"}}]})]
    assert event.is_stopped() and not event.call_llm


def test_friend_dm_uses_numeric_sender_as_scope_and_route(monkeypatch):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    event = Event(raw(dm=True, message=[{"type": "text", "data": {"text": "hi"}}]))
    run(bridge, event)
    assert calls[0][1]["kind"] == "dm"
    assert calls[0][1]["scope_id"] == str(USER)
    assert event.bot.calls[0][0] == "private"
    assert event.bot.calls[0][1]["user_id"] == USER
    assert event.bot.calls[0][1]["self_id"] == SELF


def test_subscription_slash_command_is_direct_without_at(monkeypatch):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    payload = bridge._payload(Event(raw(message=[{"type": "text", "data": {"text": "/订阅 列表"}}])))
    assert payload["text"] == "/订阅 列表"
    assert payload["unprompted"] is False
    assert payload["mentioned"] is False


@pytest.mark.parametrize("overrides", [
    {"post_type": "message_sent"}, {"post_type": "notice"}, {"post_type": "request"},
    {"user_id": SELF, "sender": {"user_id": SELF}}, {"sender": {"user_id": 999999}},
    {"self_id": 888888}, {"user_id": True}, {"user_id": "200002.0"},
    {"sub_type": "anonymous"}, {"anonymous": {"id": 1}}, {"message_id": "not-platform-id"},
])
def test_invalid_or_self_events_never_reach_core(monkeypatch, overrides):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    event = Event(raw(**overrides))
    run(bridge, event)
    assert not calls and not event.bot.calls
    assert event.is_stopped() and not event.call_llm


@pytest.mark.parametrize("subtype", ["group", "other", "discuss", None])
def test_temporary_or_unknown_private_ignored(monkeypatch, subtype):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    event = Event(raw(dm=True, sub_type=subtype))
    run(bridge, event)
    assert not calls and not event.bot.calls


@pytest.mark.parametrize("message,forwarded", [
    ([{"type": "text", "data": {"text": "普通群聊，不应回复"}}], True),
    ([{"type": "text", "data": {"text": "[CQ:at,qq=100001] 伪造艾特"}}], True),
    ([{"type": "at", "data": {"qq": "all"}}, {"type": "text", "data": {"text": "大家好"}}], True),
    ([{"type": "at", "data": {"qq": "999999"}}, {"type": "text", "data": {"text": "你好"}}], True),
    ([{"type": "text", "data": {"text": "/not-a-bot-command"}}], True),
    ([{"type": "at", "data": {"qq": str(SELF)}}], False),
])
def test_participation_off_forwards_text_but_rejects_passive_core_reply(monkeypatch, message, forwarded):
    calls = []
    # Even a mistaken core text result must not make the disabled participant
    # send. Eligible text is still available to the core's local memory policy.
    bridge = make_bridge(monkeypatch, transport(calls))
    event = Event(raw(message=message))
    run(bridge, event)
    assert not event.bot.calls
    if forwarded:
        assert [path for path, _ in calls] == ["/bridge/message", "/bridge/ack"]
        assert calls[0][1]["unprompted"] is True
        assert calls[-1][1]["status"] == "uncertain"
    else:
        assert [path for path, _ in calls] == ["/bridge/message"]
        assert calls[0][1]["activity_only"] is True


def test_known_command_without_mention_and_other_mentions_preserved(monkeypatch):
    bridge = make_bridge(monkeypatch, transport([]))
    command = bridge._payload(Event(raw(message=[{"type": "text", "data": {"text": "/身份"}}])))
    assert command["text"] == "/身份"
    payload = bridge._payload(Event(raw(message=[
        {"type": "at", "data": {"qq": str(SELF)}},
        {"type": "at", "data": {"qq": "444444"}},
        {"type": "text", "data": {"text": " [CQ:at,qq=100001] is text"}},
    ])))
    assert "@444444" in payload["text"]
    assert "[CQ:at,qq=100001]" in payload["text"]
    asyncio.run(bridge.terminate())


@pytest.mark.parametrize("config", [{"enabled": False}, {"self_id": ""}, {"core_url": "http://example.com"}])
def test_disabled_and_invalid_config_claim_adapter_without_sending(monkeypatch, config):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls), **config)
    event = Event(raw())
    run(bridge, event)
    assert not calls and not event.bot.calls and event.is_stopped()


def test_different_adapter_unclaimed(monkeypatch):
    bridge = make_bridge(monkeypatch, transport([]))
    event = Event(raw(), adapter="other-onebot")
    run(bridge, event)
    assert not event.is_stopped()


def test_dict_instead_of_sdk_event_rejected(monkeypatch):
    bridge = make_bridge(monkeypatch, transport([]))
    with pytest.raises(ValueError):
        bridge._payload(Event(dict(raw())))
    asyncio.run(bridge.terminate())


@pytest.mark.parametrize("dm", [False, True])
def test_real_record_parser_sends_base64_wav_once_no_text(monkeypatch, tmp_path, dm):
    import wave
    wav = tmp_path / "voice.wav"
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
        out.writeframes(b"\x00\x00" * 480)
    monkeypatch.setenv("AZUSA_MEDIA_ROOT", str(tmp_path))
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls, {"reply_id": "b" * 32,
        "mode": "voice", "text": "不要重复这段文字", "media_path": str(wav)}))
    event = Event(raw(dm=dm))
    run(bridge, event)
    assert len(event.bot.calls) == 1
    segments = event.bot.calls[0][1]["message"]
    assert len(segments) == 1 and segments[0]["type"] == "record"
    encoded = segments[0]["data"]["file"]
    assert encoded.startswith("base64://")
    assert base64.b64decode(encoded[9:]) == wav.read_bytes()
    assert calls[-1][1]["status"] == "sent"


@pytest.mark.parametrize("bot", [Bot(fail=True), Bot(receipt=False)])
def test_uncertain_delivery_has_no_retry_or_fallback(monkeypatch, bot):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    event = Event(raw(), bot=bot)
    run(bridge, event)
    assert len(bot.calls) == 1
    assert calls[-1][1]["status"] == "uncertain"
    assert "message_id" not in calls[-1][1]


def test_media_path_escape_and_placeholder_rejected(monkeypatch, tmp_path):
    media = tmp_path / "media"; media.mkdir()
    outside = tmp_path / "private.wav"; outside.write_bytes(b"private")
    (media / "escape.wav").symlink_to(outside)
    monkeypatch.setenv("AZUSA_MEDIA_ROOT", str(media))
    bridge = make_bridge(monkeypatch, transport([]))
    for path in [outside, media / "escape.wav"]:
        with pytest.raises(ValueError):
            bridge._message({"mode": "voice", "media_path": str(path)})
    with pytest.raises(ValueError):
        bridge._message({"mode": "text", "text": "（语音已发送。）"})
    asyncio.run(bridge.terminate())


def test_blocked_response_does_not_send_or_ack(monkeypatch):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls, {"mode": "none", "text": "", "reason": "group_not_allowed"}))
    event = Event(raw())
    run(bridge, event)
    assert len(calls) == 1 and not event.bot.calls


def write_policy(path, **changes):
    policy = {"transport": "onebot", "onebot_enabled": True, "onebot_self_id": str(SELF),
              "onebot_allowed_groups": [str(GROUP)], "onebot_allowed_dm_users": [str(USER)]}
    policy.update(changes)
    path.write_text(json.dumps(policy))
    path.chmod(0o600)


def test_power_gate_blocks_forwarding_without_changing_account_policy(monkeypatch, tmp_path):
    from azusa_bot.bot_control import BotPowerController
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    path = tmp_path / "config/local.json"
    path.parent.mkdir()
    write_policy(path)
    monkeypatch.setenv("AZUSA_ONEBOT_POLICY_FILE", str(path))
    original = path.read_bytes()
    power = BotPowerController(tmp_path, lambda: None)
    power._write("stopped")
    event = Event(raw())
    run(bridge, event)
    assert not calls and not event.bot.calls
    assert path.read_bytes() == original


def test_private_policy_refresh_overrides_plugin_without_restart(monkeypatch, tmp_path):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls), enabled=False, self_id="")
    path = tmp_path / "policy.json"
    write_policy(path)
    monkeypatch.setenv("AZUSA_ONEBOT_POLICY_FILE", str(path))

    async def go():
        first = Event(raw())
        await bridge.on_message(first)
        assert len(first.bot.calls) == 1
        write_policy(path, onebot_enabled=False)
        disabled = Event(raw())
        await bridge.on_message(disabled)
        assert not disabled.bot.calls
        write_policy(path, onebot_self_id="888888")
        mismatch = Event(raw())
        await bridge.on_message(mismatch)
        assert not mismatch.bot.calls
        write_policy(path)
        enabled_again = Event(raw())
        await bridge.on_message(enabled_again)
        assert len(enabled_again.bot.calls) == 1
        await bridge.terminate()

    asyncio.run(go())
    assert len(calls) == 6  # two core requests, authorizations and acknowledgements


@pytest.mark.parametrize("case", ["missing", "malformed", "public", "symlink", "wrong_transport", "string_true", "bad_self", "array"])
def test_invalid_policy_never_falls_back_to_enabled_plugin(monkeypatch, tmp_path, case):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls), enabled=True)
    path = tmp_path / "policy.json"
    write_policy(path)
    if case == "missing":
        path.unlink()
    elif case == "malformed":
        path.write_text("not json")
    elif case == "public":
        path.chmod(0o644)
    elif case == "symlink":
        target = tmp_path / "real.json"
        path.rename(target)
        path.symlink_to(target)
    elif case == "wrong_transport":
        write_policy(path, transport="official")
    elif case == "string_true":
        write_policy(path, onebot_enabled="true")
    elif case == "bad_self":
        write_policy(path, onebot_self_id="nickname")
    elif case == "array":
        path.write_text("[]")
    monkeypatch.setenv("AZUSA_ONEBOT_POLICY_FILE", str(path))
    event = Event(raw())
    run(bridge, event)
    assert not calls and not event.bot.calls and event.is_stopped()


@pytest.mark.parametrize("participate", [False, True])
def test_group_text_is_forwarded_without_forcing_a_reply(monkeypatch, participate):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls, {"mode": "none", "text": ""}),
                         group_chat_enabled=participate)
    event = Event(raw(message=[{"type": "text", "data": {"text": "最近在玩什么游戏？"}}]))
    run(bridge, event)
    assert len(calls) == 1 and not event.bot.calls
    assert calls[0][1]["mentioned"] is False
    assert calls[0][1]["unprompted"] is True
    assert calls[0][1]["text"] == "最近在玩什么游戏？"
    assert event.is_stopped() and not event.call_llm


@pytest.mark.parametrize("reason", ["ignored", "rate_limited", "quiet", "merged"])
def test_silent_core_result_with_ledger_identity_is_never_acknowledged(monkeypatch, reason):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls, {"mode": "none", "text": "",
                         "reply_id": "already-recorded-by-core", "reason": reason}),
                         group_chat_enabled=True)
    event = Event(raw(message=[{"type": "text", "data": {"text": "普通聊天"}}]))
    run(bridge, event)
    assert len(calls) == 1 and calls[0][0] == "/bridge/message"
    assert not event.bot.calls


@pytest.mark.parametrize("message,mentioned,reply_to_id", [
    ([{"type": "text", "data": {"text": "[CQ:at,qq=100001] [CQ:reply,id=-88]"}}], False, None),
    ([{"type": "reply", "data": {"id": -88}}, {"type": "text", "data": {"text": "你刚说什么？"}}], False, "-88"),
    ([{"type": "at", "data": {"qq": str(SELF)}}, {"type": "text", "data": {"text": "你好"}}], True, None),
])
def test_only_real_segments_produce_trusted_mention_and_reply_metadata(monkeypatch, message, mentioned, reply_to_id):
    bridge = make_bridge(monkeypatch, transport([]), group_chat_enabled=True)
    payload = bridge._payload(Event(raw(message=message)))
    assert payload["mentioned"] is mentioned
    assert payload["reply_to_id"] == reply_to_id
    assert payload["unprompted"] is (not mentioned)
    asyncio.run(bridge.terminate())


@pytest.mark.parametrize("targets,text,mentioned,other_mentioned", [
    (["444444"], "你觉得呢？", False, True),
    (["all"], "大家怎么想？", False, True),
    ([str(SELF)], "你觉得呢？", True, False),
    ([SELF], "你觉得呢？", True, False),
    ([str(SELF), "444444"], "你们怎么想？", True, True),
    ([], "[CQ:at,qq=444444] [CQ:at,qq=all] @其他人", False, False),
    ([str(SELF)], "[CQ:at,qq=444444] 这只是文字", True, False),
])
def test_other_mentioned_only_trusts_actual_at_segments(monkeypatch, targets, text, mentioned, other_mentioned):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls, {"mode": "none", "text": ""}),
                         group_chat_enabled=True)
    segments = [{"type": "at", "data": {"qq": target}} for target in targets]
    segments.append({"type": "text", "data": {"text": text}})
    event = Event(raw(message=segments))
    run(bridge, event)
    assert len(calls) == 1 and not event.bot.calls
    payload = calls[0][1]
    assert payload["mentioned"] is mentioned
    assert payload["other_mentioned"] is other_mentioned
    assert payload["unprompted"] is (not mentioned)


@pytest.mark.parametrize("sender,expected", [
    ({"user_id": USER, "card": " 群内名\n\t小明\x00\u202e ", "nickname": "ignored"}, "群内名 小明"),
    ({"user_id": USER, "card": "", "nickname": "昵称"}, "昵称"),
    ({"user_id": USER, "card": "长" * 100, "nickname": "ignored"}, "长" * 80),
    ({"user_id": USER, "card": {"user_id": SELF}, "nickname": None}, ""),
])
def test_display_name_is_bounded_label_from_sdk_sender_never_identity(monkeypatch, sender, expected):
    bridge = make_bridge(monkeypatch, transport([]))
    payload = bridge._payload(Event(raw(sender=sender)))
    assert payload["display_name"] == expected
    assert payload["user_id"] == str(USER)
    asyncio.run(bridge.terminate())


@pytest.mark.parametrize("dm", [False, True])
@pytest.mark.parametrize("kind", ["image", "record", "video", "file", "face", "forward", "reply", "at"])
def test_no_pure_media_or_mentions_are_invented_as_text(monkeypatch, dm, kind):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls, {"mode":"none","text":""}), group_chat_enabled=True)
    data = {"file": "https://untrusted.invalid/member-private", "text": "not a real text segment"}
    if kind == "reply":
        data["id"] = -777
    if kind == "at":
        data["qq"] = "999999"
    event = Event(raw(dm=dm, message=[
        {"type": "at", "data": {"qq": str(SELF)}}, {"type": kind, "data": data},
        {"type": "text", "data": {"text": " \n "}},
    ]))
    run(bridge, event)
    assert not event.bot.calls
    if dm:
        assert not calls
    else:
        assert len(calls)==1 and calls[0][0]=='/bridge/message'
        assert calls[0][1]['activity_only'] is True and calls[0][1]['text']=='' and calls[0][1]['images']==[]


def test_mixed_media_keeps_authentic_text_reply_and_native_image_separate(monkeypatch):
    bridge = make_bridge(monkeypatch, transport([]), group_chat_enabled=True)
    payload = bridge._payload(Event(raw(message=[
        {"type": "reply", "data": {"id": "-123", "text": "not trusted quote content"}},
        {"type": "image", "data": {"url": "https://untrusted.invalid/private"}},
        {"type": "text", "data": {"text": "这张图好好笑"}},
    ])))
    assert payload["text"] == "这张图好好笑"
    assert payload["reply_to_id"] == "-123"
    assert payload["unprompted"] is True
    assert payload["images"] == ["https://untrusted.invalid/private"]
    asyncio.run(bridge.terminate())


@pytest.mark.parametrize("dm", [False, True])
def test_pure_native_image_reaches_core_without_invented_text(monkeypatch, dm):
    bridge = make_bridge(monkeypatch, transport([]), group_chat_enabled=True)
    payload = bridge._payload(Event(raw(dm=dm, message=[
        {"type": "at", "data": {"qq": str(SELF)}},
        {"type": "image", "data": {"url": "https://gchat.qpic.cn/gchatpic_new/example"}},
    ])))
    assert payload["text"] == "" and payload["mentioned"] is True
    assert payload["images"] == ["https://gchat.qpic.cn/gchatpic_new/example"]
    assert payload["unprompted"] is False
    asyncio.run(bridge.terminate())


def test_image_urls_bounded_deduplicated_and_never_taken_from_text_or_file(monkeypatch):
    bridge = make_bridge(monkeypatch, transport([]), group_chat_enabled=True)
    payload = bridge._payload(Event(raw(message=[
        {"type": "text", "data": {"text": "https://gchat.qpic.cn/text [CQ:image,file=fake]"}},
        {"type": "file", "data": {"url": "https://gchat.qpic.cn/file"}},
        {"type": "image", "data": {"file": "file:///etc/passwd"}},
        *[{"type": "image", "data": {"url": "https://gchat.qpic.cn/" + suffix}}
          for suffix in ["one", "one", "two", "three", "four"]],
    ])))
    assert payload["images"] == ["https://gchat.qpic.cn/" + suffix for suffix in ["one", "two", "three"]]
    assert payload["text"].startswith("https://gchat.qpic.cn/text")
    asyncio.run(bridge.terminate())


def test_unlisted_image_scope_is_not_forwarded(monkeypatch):
    bridge = make_bridge(monkeypatch, transport([]), allowed_groups=[])
    assert bridge._payload(Event(raw(message=[
        {"type": "image", "data": {"url": "https://gchat.qpic.cn/private"}},
    ]))) is None
    asyncio.run(bridge.terminate())


@pytest.mark.parametrize("reference", [None, True, "nickname", "[CQ:reply,id=1]", 1.5])
def test_malformed_reply_metadata_never_reaches_core(monkeypatch, reference):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls), group_chat_enabled=True)
    event = Event(raw(message=[{"type": "reply", "data": {"id": reference}},
                               {"type": "text", "data": {"text": "hi"}}]))
    run(bridge, event)
    assert not calls and not event.bot.calls


@pytest.mark.parametrize("dm,allowlists", [
    (False, {"allowed_groups": []}), (False, {"allowed_groups": ["999999"]}),
    (True, {"allowed_dm_users": []}), (True, {"allowed_dm_users": ["999999"]}),
])
@pytest.mark.parametrize("text", ["群里随便聊聊", "/身份"])
def test_unlisted_scopes_never_reach_core_even_when_mentioned_or_commands(monkeypatch, dm, allowlists, text):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls), group_chat_enabled=True, **allowlists)
    event = Event(raw(dm=dm, message=[{"type": "at", "data": {"qq": str(SELF)}},
                                    {"type": "text", "data": {"text": text}}]))
    run(bridge, event)
    assert not calls and not event.bot.calls and event.is_stopped()


@pytest.mark.parametrize("changes", [
    {"onebot_allowed_groups": "300003"}, {"onebot_allowed_groups": [True]},
    {"onebot_allowed_groups": ["nickname"]}, {"onebot_allowed_dm_users": {}},
])
def test_malformed_private_allowlist_fails_closed(monkeypatch, tmp_path, changes):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls), group_chat_enabled=True)
    path = tmp_path / "policy.json"
    write_policy(path, group_chat_enabled=True, **changes)
    monkeypatch.setenv("AZUSA_ONEBOT_POLICY_FILE", str(path))
    event = Event(raw())
    run(bridge, event)
    assert not calls and not event.bot.calls


def test_private_group_participation_flag_refreshes_without_restart(monkeypatch, tmp_path):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls, {"mode": "none", "text": ""}),
                         group_chat_enabled=True)
    path = tmp_path / "policy.json"
    write_policy(path)
    monkeypatch.setenv("AZUSA_ONEBOT_POLICY_FILE", str(path))

    async def go():
        for flag in (False, True, "true", False, True):
            write_policy(path, group_chat_enabled=flag)
            await bridge.on_message(Event(raw(message=[{"type": "text", "data": {"text": "普通聊天"}}])))
        write_policy(path, group_chat_enabled=True, onebot_allowed_groups=[])
        await bridge.on_message(Event(raw()))
        await bridge.terminate()

    asyncio.run(go())
    # Participation only controls speaking; all eligible text reaches the local
    # core for its independent collection policy. The unlisted group is blocked.
    assert len(calls) == 5
    assert all(path == "/bridge/message" and body["unprompted"] for path, body in calls)


def test_listening_privacy_command_works_with_group_participation_disabled(monkeypatch):
    calls = []
    bridge = make_bridge(monkeypatch, transport(calls))
    event = Event(raw(message=[{"type": "text", "data": {"text": "/旁听 关"}}]))
    run(bridge, event)
    assert calls[0][1]["text"] == "/旁听 关"
    assert calls[0][1]["mentioned"] is False and calls[0][1]["unprompted"] is False


@pytest.mark.parametrize("unprompted,changes,should_send", [
    (True, {"group_chat_enabled": False}, False),
    (False, {"group_chat_enabled": False}, True),
    (True, {"onebot_allowed_groups": []}, False),
    (False, {"onebot_allowed_groups": []}, False),
    (True, {"onebot_enabled": False}, False),
    (True, {"onebot_self_id": "888888"}, False),
])
def test_policy_revocation_during_generation_prevents_send(monkeypatch, tmp_path, unprompted, changes, should_send):
    calls = []
    path = tmp_path / "policy.json"
    write_policy(path, group_chat_enabled=True)

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/bridge/message":
            write_policy(path, **{"group_chat_enabled": True, **changes})
            return httpx.Response(200, json={"reply_id": "r" * 32, "mode": "text", "text": "接一句"})
        if request.url.path == "/bridge/reply/authorize":
            return httpx.Response(200, json={"allowed": True})
        return httpx.Response(200, json={})

    bridge = make_bridge(monkeypatch, handler)
    monkeypatch.setenv("AZUSA_ONEBOT_POLICY_FILE", str(path))
    event = Event(raw(message=[{"type": "text", "data": {"text": "普通聊天"}}] if unprompted else None))
    run(bridge, event)
    assert len(event.bot.calls) == int(should_send)
    assert calls[-1][1]["status"] == ("sent" if should_send else "uncertain")
    assert ("message_id" in calls[-1][1]) is should_send


def test_concurrent_events_reach_core_while_debounce_is_pending(monkeypatch):
    calls, arrived = [], set()
    release = asyncio.Event()

    async def handler(request):
        data = json.loads(request.content)
        calls.append((request.url.path, data))
        if request.url.path == "/bridge/ack":
            return httpx.Response(200, json={})
        if request.url.path == "/bridge/reply/authorize":
            return httpx.Response(200, json={"allowed": True})
        arrived.add(data["event_id"])
        if len(arrived) == 3:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=2)
        # Simulate a core batch: only one waiting event receives the output.
        reply = ({"reply_id": "batch", "mode": "text", "text": "只接一句"}
                 if data["event_id"] == "-3" else {"mode": "none", "text": ""})
        return httpx.Response(200, json=reply)

    bridge = make_bridge(monkeypatch, handler, group_chat_enabled=True,
                         allowed_groups=[str(GROUP), "400004"])
    events = [Event(raw(message_id=-number, group_id=GROUP if number < 3 else 400004,
                        message=[{"type": "text", "data": {"text": f"消息{number}"}}]))
              for number in (1, 2, 3)]

    async def go():
        try:
            await asyncio.wait_for(asyncio.gather(*(bridge.on_message(event) for event in events)), timeout=4)
        finally:
            await bridge.terminate()

    asyncio.run(go())
    assert arrived == {"-1", "-2", "-3"}
    assert sum(len(event.bot.calls) for event in events) == 1
    assert len([path for path, _ in calls if path == "/bridge/ack"]) == 1
    assert all(event.is_stopped() and not event.call_llm for event in events)


def test_core_timeout_during_passive_context_is_silent_and_never_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("private payload must not be logged", request=request)

    bridge = make_bridge(monkeypatch, handler, group_chat_enabled=True)
    event = Event(raw(message=[{"type": "text", "data": {"text": "普通聊天"}}]))
    run(bridge, event)
    assert calls == ["/bridge/message"] and not event.bot.calls


@pytest.mark.parametrize("authorization", [{"allowed": False}, {}, {"allowed": "true"}, None])
def test_switched_profile_or_failed_authorization_suppresses_qq_send(monkeypatch, authorization):
    calls = []
    normal = transport(calls)
    def handler(request):
        if request.url.path == "/bridge/reply/authorize":
            calls.append((request.url.path, json.loads(request.content)))
            if authorization is None:
                raise httpx.ReadTimeout("loopback unavailable", request=request)
            return httpx.Response(200, json=authorization)
        return normal(request)
    bridge = make_bridge(monkeypatch, handler)
    event = Event(raw())
    run(bridge, event)
    assert not event.bot.calls
    assert [p for p, _ in calls] == ["/bridge/message", "/bridge/reply/authorize", "/bridge/ack"]
    assert calls[-1][1]["status"] == "uncertain"


def test_per_group_policy_rechecks_specific_target_and_cannot_expand_allowlist(monkeypatch,tmp_path):
    calls=[]
    bridge=make_bridge(monkeypatch,transport(calls))
    path=tmp_path/'local.json';write_policy(path,group_chat_enabled=False)
    monkeypatch.setenv('AZUSA_ONEBOT_POLICY_FILE',str(path))
    groups=tmp_path/'groups.json'
    scope=f'onebot:{SELF}:group:{GROUP}'
    groups.write_text(json.dumps({scope:{'group_chat_enabled':True,'group_idle_enabled':True},f'onebot:{SELF}:group:99999':{'group_chat_enabled':True},f'onebot:99999:group:{GROUP}':{'group_chat_enabled':False}}));groups.chmod(0o600)
    p=bridge._policy()
    assert p['chat_groups']==frozenset([str(GROUP)]) and p['idle_groups']==frozenset([str(GROUP)])
    message=[{'type':'text','data':{'text':'普通群聊测试'}}]
    event=Event(raw(message=message));asyncio.run(bridge.on_message(event));assert len(event.bot.calls)==1
    groups.write_text(json.dumps({scope:{'group_chat_enabled':False}}))
    write_policy(path,group_chat_enabled=True)
    event=Event(raw(message=message));asyncio.run(bridge.on_message(event));assert not event.bot.calls
    groups.chmod(0o644)
    with pytest.raises(ValueError):bridge._policy()
    asyncio.run(bridge.client.aclose())


@pytest.mark.parametrize('failure',[None,'second_send','second_authorize','lost_core_ack'])
def test_segmented_reply_receipts_stop_without_retry(monkeypatch,failure):
    calls=[]
    parts=['第一句已经完整说完了。','第二句是另外一个意思！','第三句是最后的补充。']
    reply={'reply_id':'b'*32,'mode':'text','text':''.join(parts),'segments':parts,'segment_interval_seconds':0}
    def handler(request):
        data=json.loads(request.content);calls.append((request.url.path,data))
        if request.url.path=='/bridge/message':return httpx.Response(200,json=reply)
        if request.url.path.endswith('segment-authorize'):
            return httpx.Response(200,json={'allowed':not(failure=='second_authorize' and data['segment_index']==1)})
        if request.url.path.endswith('segment-ack') and failure=='lost_core_ack':raise httpx.ReadTimeout('private diagnostic')
        return httpx.Response(200,json={'ok':True})
    class SegBot(Bot):
        async def _send(self,route,**kwargs):
            self.calls.append((route,kwargs))
            if failure=='second_send' and len(self.calls)==2:raise TimeoutError('private diagnostic')
            return {'message_id':-900-len(self.calls)}
    bridge=make_bridge(monkeypatch,handler);event=Event(raw(),bot=SegBot());run(bridge,event)
    assert len(event.bot.calls)==({None:3,'second_send':2,'second_authorize':1,'lost_core_ack':1}[failure])
    assert calls[-1][0]=='/bridge/reply/segment-finish'
    assert not any(path=='/bridge/ack' for path,_ in calls)
    acks=[data for path,data in calls if path.endswith('segment-ack')]
    assert acks[0]['status']=='sent'
    if failure=='second_send':assert acks[-1]['status']=='uncertain' and 'message_id' not in acks[-1]
    assert event.is_stopped() and not event.call_llm


def test_segments_keep_explicit_at_markup_as_plain_text(monkeypatch):
    calls=[];parts=['[CQ:at,qq=all] 第一段文字。','后面继续另一段文字。']
    def handler(request):
        calls.append(request.url.path)
        if request.url.path=='/bridge/message':return httpx.Response(200,json={'reply_id':'c'*32,'mode':'text','text':''.join(parts),'segments':parts,'segment_interval_seconds':0})
        return httpx.Response(200,json={'allowed':True,'ok':True})
    bridge=make_bridge(monkeypatch,handler);event=Event(raw());run(bridge,event)
    assert len(event.bot.calls)==2
    assert event.bot.calls[0][1]['message']==[{'type':'text','data':{'text':parts[0]}}]
