"""Exercise the actual pinned CQHttp server without a real QQ connection."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("aiocqhttp")
from aiocqhttp import CQHttp
from quart.testing.connections import WebsocketResponseError

spec = importlib.util.spec_from_file_location(
    "xzbot_ingress_guard", Path(__file__).parents[1] / "scripts/onebot_security.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.asyncio
async def test_all_http_events_rejected_before_dispatch_even_with_valid_ws_token():
    bot = CQHttp(access_token="synthetic-test-only-ws-token")
    bot._handle_event = AsyncMock(return_value={})
    guard.restrict_to_reverse_websocket(bot)
    client = bot.server_app.test_client()
    for headers in ({}, {"Authorization": "Bearer synthetic-test-only-ws-token"},
                    {"X-Self-ID": "10001", "Content-Type": "application/json"}):
        result = await client.post("/", json={"user_id": 20001, "message": "/身份"}, headers=headers)
        assert result.status_code == 403
    bot._handle_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization,code", [(None, 401), ("Bearer wrong", 403)])
async def test_reverse_websocket_still_requires_its_token(authorization, code):
    bot = CQHttp(access_token="synthetic-test-only-ws-token")
    guard.restrict_to_reverse_websocket(bot)
    headers = {"X-Client-Role": "event", "X-Self-ID": "10001"}
    if authorization:
        headers["Authorization"] = authorization
    with pytest.raises(WebsocketResponseError) as exc:
        async with bot.server_app.test_client().websocket("/ws", headers=headers) as ws:
            await ws.receive()
    assert exc.value.response.status_code == code


@pytest.mark.asyncio
async def test_authenticated_reverse_websocket_remains_usable():
    bot = CQHttp(access_token="synthetic-test-only-ws-token")
    guard.restrict_to_reverse_websocket(bot)
    headers = {"Authorization": "Bearer synthetic-test-only-ws-token",
               "X-Client-Role": "event", "X-Self-ID": "10001"}
    # Connection acceptance itself is enough; do not dispatch an event.
    async with bot.server_app.test_client().websocket("/ws", headers=headers) as ws:
        await ws.send("not-json")



@pytest.mark.asyncio
async def test_hidden_messages_dropped_before_astrbot_conversion(monkeypatch):
    pytest.importorskip('astrbot')
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
    original=AsyncMock(return_value='converted')
    monkeypatch.setattr(AiocqhttpAdapter,'convert_message',original)
    monkeypatch.setattr(AiocqhttpAdapter,'__init__',lambda *a,**k:None)
    monkeypatch.setattr(AiocqhttpAdapter,'_xzbot_ws_only',False,raising=False)
    guard.install_adapter_guard()
    adapter=object()
    event={'post_type':'message','message':[
        {'type':'at','data':{'qq':'10001'}},
        {'type':'text','data':{'text':' \n（'}},
        {'type':'text','data':{'text':'（不该解析附件'}},
        {'type':'image','data':{'url':'https://not-fetched.invalid/image.jpg'}}]}
    assert await AiocqhttpAdapter.convert_message(adapter,event) is None
    original.assert_not_called()
    event['message']=[{'type':'text','data':{'text':'正常聊天'}}]
    assert await AiocqhttpAdapter.convert_message(adapter,event)=='converted'
    original.assert_awaited_once_with(adapter,event)
