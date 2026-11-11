"""Restrict the pinned CQHttp receiver to its authenticated reverse WebSocket.

CQHttp registers a separate HTTP event endpoint unconditionally. Its HTTP
signature secret is independent of the reverse-WS access token. XZBot never
uses HTTP push, so reject that entire surface before parsing an event.
"""
from __future__ import annotations
from azusa_bot.message_privacy import hidden_segments


def restrict_to_reverse_websocket(bot):
    from quart import abort

    @bot.server_app.before_request
    async def reject_http_events():
        abort(403)


def install_adapter_guard():
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter

    if getattr(AiocqhttpAdapter, "_xzbot_ws_only", False):
        return
    original = AiocqhttpAdapter.__init__
    original_convert = AiocqhttpAdapter.convert_message

    def guarded_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        restrict_to_reverse_websocket(self.bot)

    async def guarded_convert(self, event):
        # Drop before AstrBot's debug logging, quote API calls and media parsing.
        if event.get("post_type") == "message" and hidden_segments(event.get("message")):
            return None
        return await original_convert(self, event)

    AiocqhttpAdapter.__init__ = guarded_init
    AiocqhttpAdapter.convert_message = guarded_convert
    AiocqhttpAdapter._xzbot_ws_only = True
