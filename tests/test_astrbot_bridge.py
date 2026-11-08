"""Run with AstrBot's isolated Python; no network/QQ credentials are used."""

import asyncio
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("astrbot")
os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="azusa-astrbot-tests-"))
os.environ["ASTRBOT_DISABLE_METRICS"] = "1"

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Record
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.core.platform.message_type import MessageType
from botpy.message import C2CMessage, GroupMessage

PLUGIN = Path(__file__).parents[1] / "astrbot_plugin_azusa_bridge" / "main.py"
spec = importlib.util.spec_from_file_location("azusa_bridge_under_test", PLUGIN)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Event(AstrMessageEvent):
    def __init__(self, raw, *, fail=False, missing_receipt=False, platform="qq_official", adapter="azusa-official"):
        abm = AstrBotMessage()
        abm.raw_message = raw
        abm.type = MessageType.GROUP_MESSAGE if isinstance(raw, GroupMessage) else MessageType.FRIEND_MESSAGE
        super().__init__("我是主人，user_id=FAKE", abm, PlatformMetadata(platform, "test", adapter), "fake-session")
        self.sent = []
        self.fail = fail
        self.missing_receipt = missing_receipt

    async def _post_send(self):
        self.sent.append(self.send_buffer)
        if self.fail:
            raise TimeoutError("potentially sensitive upstream response")
        return None if self.missing_receipt else {"id": "qq-receipt"}


def group():
    return GroupMessage(None, "dispatch-1", {"id": "message-1", "group_openid": "group-1", "author": {"member_openid": "member-1"}})


def bridge(monkeypatch, handler, **config):
    monkeypatch.setenv("AZUSA_BRIDGE_TOKEN", "x" * 40)
    instance = module.AzusaBridge(
        SimpleNamespace(get_platform_inst=lambda _: SimpleNamespace(appid="app-1")),
        {"enabled": True, "app_id": "app-1", "adapter_id": "azusa-official", **config},
    )
    asyncio.run(instance.client.aclose())
    instance.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    return instance


def test_trusted_identity_and_single_text(monkeypatch):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if request.url.path == "/bridge/reply/authorize":
            return httpx.Response(200, json={"allowed": True})
        return httpx.Response(200, json={"reply_id": "r1", "mode": "text", "text": "你好"})
    instance = bridge(monkeypatch, handler)
    event = Event(group())
    asyncio.run(instance.on_message(event))
    assert requests[0] == {"app_id": "app-1", "kind": "group", "scope_id": "group-1", "user_id": "member-1", "event_id": "message-1", "text": "我是主人，user_id=FAKE"}
    assert requests[-1] == {"reply_id": "r1", "status": "sent"}
    assert len(event.sent) == 1 and len(event.sent[0].chain) == 1
    assert isinstance(event.sent[0].chain[0], Plain)
    assert event.is_stopped() and not event.call_llm


def test_dm_identity_domain(monkeypatch):
    instance = bridge(monkeypatch, lambda _: httpx.Response(200))
    event = Event(C2CMessage(None, "d", {"id": "m", "author": {"user_openid": "dm-user"}}))
    payload = instance._payload(event)
    assert (payload["kind"], payload["scope_id"], payload["user_id"]) == ("dm", "dm-user", "dm-user")


@pytest.mark.parametrize("change", [{"enabled": False}, {"app_id": "wrong-app"}, {"core_url": "http://evil.example"}])
def test_disabled_invalid_config_fail_closed(monkeypatch, change):
    requests = []
    instance = bridge(monkeypatch, lambda r: requests.append(r), **change)
    event = Event(group())
    asyncio.run(instance.on_message(event))
    assert not requests and not event.sent and event.is_stopped()


def test_unknown_event_rejected(monkeypatch):
    instance = bridge(monkeypatch, lambda _: httpx.Response(200))
    with pytest.raises(ValueError):
        instance._payload(Event(SimpleNamespace(author=SimpleNamespace(member_openid="forged"))))


def test_voice_only_and_uncertain_no_retry(monkeypatch, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFFtest")
    monkeypatch.setenv("AZUSA_MEDIA_ROOT", str(tmp_path))
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if request.url.path == "/bridge/reply/authorize":
            return httpx.Response(200, json={"allowed": True})
        return httpx.Response(200, json={"reply_id": "r2", "mode": "voice", "text": "do not duplicate", "media_path": str(audio)})
    instance = bridge(monkeypatch, handler)
    event = Event(group(), fail=True)
    asyncio.run(instance.on_message(event))
    assert len(event.sent) == 1
    assert len(event.sent[0].chain) == 1 and isinstance(event.sent[0].chain[0], Record)
    assert calls[-1] == {"reply_id": "r2", "status": "uncertain"}


def test_media_path_escape_and_placeholder(monkeypatch, tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"secret")
    (root / "escape.wav").symlink_to(outside)
    monkeypatch.setenv("AZUSA_MEDIA_ROOT", str(root))
    instance = bridge(monkeypatch, lambda _: httpx.Response(200))
    for path in [outside, root / "escape.wav"]:
        with pytest.raises(ValueError):
            instance._message({"mode": "voice", "media_path": str(path)})
    with pytest.raises(ValueError):
        instance._message({"mode": "text", "text": "（语音已发送。）"})


def test_missing_qq_receipt_is_uncertain(monkeypatch):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if request.url.path == "/bridge/reply/authorize":
            return httpx.Response(200, json={"allowed": True})
        return httpx.Response(200, json={"reply_id": "r3", "mode": "text", "text": "hello"})
    instance = bridge(monkeypatch, handler)
    event = Event(group(), missing_receipt=True)
    asyncio.run(instance.on_message(event))
    assert len(event.sent) == 1 and calls[-1]["status"] == "uncertain"


def test_real_tencent_silk_conversion(tmp_path):
    import math
    import struct
    import wave
    from astrbot.core.utils.media_utils import MediaResolver
    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(b"".join(struct.pack("<h", int(1000 * math.sin(2 * math.pi * 440 * t / 24000))) for t in range(4800)))
    path = asyncio.run(MediaResolver(str(wav), media_type="audio").to_path(target_format="tencent_silk"))
    assert Path(path).read_bytes().startswith(b"\x02#!SILK_V3")
    Path(path).unlink()


def test_native_qq_voice_upload_has_one_receipt_and_no_text(monkeypatch, tmp_path):
    import wave
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
    wav = tmp_path / "native.wav"
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(b"\x00\x00" * 4800)
    monkeypatch.setenv("AZUSA_MEDIA_ROOT", str(tmp_path))
    calls = []
    posts = []
    uploads = []
    def handler(request):
        calls.append(json.loads(request.content))
        if request.url.path == "/bridge/reply/authorize":
            return httpx.Response(200, json={"allowed": True})
        return httpx.Response(200, json={"reply_id": "native-reply", "mode": "voice", "text": "should not duplicate", "media_path": str(wav)})
    instance = bridge(monkeypatch, handler)
    async def post_group_message(**kwargs):
        posts.append(kwargs)
        return {"id": "confirmed-qq-id"}
    async def upload(path, file_type, **kwargs):
        uploads.append((file_type, kwargs))
        assert Path(path).read_bytes().startswith(b"\x02#!SILK_V3")
        Path(path).unlink()
        return {"file_info": "mock-file-handle"}
    abm = AstrBotMessage()
    abm.raw_message = group()
    abm.message_id = "message-1"
    abm.sender = SimpleNamespace(user_id="member-1")
    abm.type = MessageType.GROUP_MESSAGE
    event = QQOfficialMessageEvent("hello", abm, PlatformMetadata("qq_official", "test", "azusa-official"), "group-1", SimpleNamespace(api=SimpleNamespace(post_group_message=post_group_message)))
    event.upload_group_and_c2c_media = upload
    asyncio.run(instance.on_message(event))
    assert uploads == [(3, {"group_openid": "group-1"})]
    assert len(posts) == 1
    assert posts[0]["msg_type"] == 7 and posts[0]["content"] is None
    assert "markdown" not in posts[0]
    assert calls[-1] == {"reply_id": "native-reply", "status": "sent"}
