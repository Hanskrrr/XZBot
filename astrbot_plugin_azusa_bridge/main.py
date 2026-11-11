"""Official QQ transport only; identity and policy are enforced by Azusa core."""

import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from azusa_bot.bot_control import activity_allowed
from azusa_bot.message_privacy import hidden_message
from botpy.message import C2CMessage, GroupMessage

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star


class AzusaBridge(Star):
    """A single-output bridge for one explicitly selected official QQ adapter."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(240.0, connect=3.0),
            trust_env=False,
            follow_redirects=False,
        )

    async def terminate(self):
        await self.client.aclose()

    def _token(self):
        value = os.environ.get("AZUSA_BRIDGE_TOKEN", "").strip()
        if not value:
            path = Path(os.environ.get("AZUSA_BRIDGE_TOKEN_FILE", ""))
            if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ValueError("Missing private bridge token file")
            value = path.read_text().strip()
        if len(value) < 32:
            raise ValueError("Invalid bridge token")
        return value

    def _endpoint(self):
        base = str(self.config.get("core_url", "http://127.0.0.1:6196")).rstrip("/")
        parsed = urlsplit(base)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Core must be a loopback HTTP endpoint")
        return base

    def _power_allowed(self):
        filename = os.environ.get("AZUSA_BRIDGE_TOKEN_FILE", "")
        return not filename or activity_allowed(Path(filename).parent.parent)

    def _payload(self, event):
        # The display name, text, synthetic session ID, and prompt never grant identity.
        platform = self.context.get_platform_inst(event.get_platform_id())
        app_id = str(getattr(platform, "appid", ""))
        if not app_id or app_id != str(self.config.get("app_id", "")):
            raise ValueError("QQ application mismatch")
        raw = event.message_obj.raw_message
        if isinstance(raw, GroupMessage):
            kind = "group"
            scope_id = raw.group_openid
            user_id = raw.author.member_openid
        elif isinstance(raw, C2CMessage):
            kind = "dm"
            scope_id = user_id = raw.author.user_openid
        else:
            # QQ guild channels and guild DMs have different identity domains.
            raise ValueError("Unsupported official QQ event")
        if not all(isinstance(v, str) and v.strip() for v in (scope_id, user_id, raw.id)):
            raise ValueError("Missing platform identity")
        return {
            "app_id": app_id,
            "kind": kind,
            "scope_id": scope_id,
            "user_id": user_id,
            "event_id": raw.id,
            "text": event.get_message_str(),
        }

    def _message(self, reply):
        mode = reply.get("mode")
        if mode == "none":
            return None
        if mode == "text":
            text = reply.get("text", "")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Empty text reply")
            if text.strip() in {"（语音已发送。）", "（语音已发送）", "语音已发送。", "语音已发送"}:
                raise ValueError("Placeholder reply rejected")
            return MessageChain(chain=[Plain(text=text)], use_markdown_=False)
        if mode not in {"voice", "image"}:
            raise ValueError("Unsupported reply mode")
        media_root = os.environ.get("AZUSA_MEDIA_ROOT", "")
        if not media_root:
            raise ValueError("Media root not configured")
        root = Path(media_root).resolve(strict=True)
        path = Path(reply.get("media_path", "")).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Media outside configured directory")
        if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError("Invalid media size")
        if mode == "voice":
            if path.suffix.lower() not in {".wav", ".mp3", ".silk"}:
                raise ValueError("Unsupported audio type")
            return MessageChain(chain=[Record(file=str(path), url=str(path))], use_markdown_=False)
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            raise ValueError("Unsupported image type")
        return MessageChain(chain=[Image.fromFileSystem(str(path))], use_markdown_=False)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != "qq_official":
            return
        if event.get_platform_id() != self.config.get("adapter_id", "azusa-official"):
            return
        # Claim this adapter even when disabled or failing. Never fall through to
        # AstrBot's LLM, default persona, image resources or TTS decorators.
        event.should_call_llm(False)
        event.stop_event()
        if not self.config.get("enabled", False):
            return
        reply_id, claim_token = None, None
        ack_status = "cancelled"
        try:
            base = self._endpoint()
            headers = {"Authorization": "Bearer " + self._token()}
            payload = self._payload(event)
            if hidden_message(payload["text"]):
                return
            if not self._power_allowed():
                return
            if not payload["text"].strip():
                return
            response = await self.client.post(base + "/bridge/message", json=payload, headers=headers)
            response.raise_for_status()
            reply = response.json()
            if not isinstance(reply, dict):
                raise ValueError("Invalid core reply")
            if reply.get("mode") == "none":
                return
            reply_id = reply.get("reply_id")
            if not isinstance(reply_id, str) or not reply_id:
                raise ValueError("Missing reply identity")
            message = self._message(reply)
            authorization = await self.client.post(base + "/bridge/reply/authorize",
                headers={**headers, "X-XZBot-Claim-Receipts": "1"}, json={"reply_id": reply_id}, timeout=10)
            authorization.raise_for_status()
            allowed = authorization.json()
            if not isinstance(allowed, dict) or allowed.get("allowed") is not True:
                reply_id = None
                return
            claim_token = allowed.get("claim_token")
            if not self._power_allowed():
                return
            # One bridge send only. QQ's upstream adapter has internal retries;
            # a timeout must never trigger a second text/voice fallback here.
            # AstrBot 4.29.4's public send() discards the QQ receipt and may
            # return normally after a swallowed conversion/upload failure.
            # Use the same pinned adapter pipeline while retaining receipt.
            event.send_buffer = message
            ack_status = "uncertain"
            receipt = await event._post_send()
            receipt_id = receipt.get("id") if isinstance(receipt, dict) else getattr(receipt, "id", None)
            if not receipt_id:
                raise ValueError("QQ delivery receipt unavailable")
            ack_status = "sent"
        except Exception:
            # Deliberately omit exception text, request bodies and response bodies.
            logger.warning("XZBot bridge failed; delivery requires local review")
        finally:
            if reply_id:
                try:
                    receipt = {"reply_id": reply_id, "status": ack_status}
                    if claim_token:
                        receipt["claim_token"] = claim_token
                    ack = await self.client.post(base + "/bridge/ack", headers=headers, json=receipt)
                    ack.raise_for_status()
                except Exception:
                    logger.warning("XZBot delivery acknowledgement unavailable")
            event.stop_event()
