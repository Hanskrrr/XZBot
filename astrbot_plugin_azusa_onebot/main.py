"""Receipt-confirmed NapCat bridge, including bounded text segments."""

import json
import asyncio
import base64
from contextlib import suppress
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from azusa_bot.bot_control import activity_allowed
from aiocqhttp import Event

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


COMMANDS = frozenset({
    "/帮助", "/隐私", "/身份", "/记忆", "/记住", "/群记住", "/事件",
    "/关系", "/忘记", "/纠正", "/忘记我", "/语音", "/文字", "/表情", "/旁听", "/订阅",
})


def numeric_id(value):
    """Canonical positive platform ID; booleans/floats/display strings are invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Invalid numeric identity")
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("Invalid numeric identity")
    return value


def message_id(value):
    # OneBot message IDs can be negative. Never use message text as an ID fallback.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Invalid message identity")
    value = str(value)
    if not re.fullmatch(r"-?[0-9]{1,20}", value):
        raise ValueError("Invalid message identity")
    return value


class AzusaOneBot(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(240.0, connect=3.0), trust_env=False, follow_redirects=False,
        )
        self._notification_task = None
        self._idle_task = None
        self._status_task = None
        self._delivery_locks = [asyncio.Lock() for _ in range(64)]

    async def initialize(self):
        self._notification_task = asyncio.create_task(self._notification_loop())
        self._idle_task = asyncio.create_task(self._idle_loop())
        self._status_task = asyncio.create_task(self._status_loop())

    async def terminate(self):
        if self._status_task:
            self._status_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._status_task
        if self._idle_task:
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
        if self._notification_task:
            self._notification_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._notification_task
        await self.client.aclose()

    async def _status_loop(self):
        while True:
            try:
                await self._status_tick()
            except Exception:
                # Core detects expired reports; never log upstream payloads or retry login.
                pass
            await asyncio.sleep(15)

    async def _status_tick(self):
        policy = self._policy()
        if not policy['self_id']:
            return
        platform = self.context.get_platform_inst(self.config.get('adapter_id','azusa-onebot'))
        bot = platform.get_client() if platform is not None and platform.meta().name=='aiocqhttp' else None
        connected = bot is not None and policy['self_id'] in getattr(bot,'_wsr_api_clients',{})
        online = None
        if connected:
            try:
                status = await asyncio.wait_for(bot.get_status(self_id=int(policy['self_id'])),timeout=4)
                if isinstance(status,dict) and type(status.get('online')) is bool:
                    online = status['online']
            except Exception:
                pass
        result = await self.client.post(self._endpoint()+'/bridge/connection-status',
            headers={'Authorization':'Bearer '+self._token()},
            json={'self_id':policy['self_id'],'connected':connected,'online':online},timeout=5)
        result.raise_for_status()

    async def _notification_loop(self):
        while True:
            try:
                await self._notification_tick()
            except Exception:
                # Never include payloads, remote titles, account IDs or tokens.
                logger.warning("XZBot subscription delivery check unavailable")
            await asyncio.sleep(5)

    async def _idle_loop(self):
        while True:
            # Only a cheap local eligibility check; the core spends a model
            # request after a new human activity cycle becomes sufficiently idle.
            await asyncio.sleep(60)
            try:
                await self._idle_tick()
            except Exception:
                logger.warning("XZBot idle participation check unavailable")

    async def _idle_tick(self):
        policy = self._policy()
        if not policy['enabled'] or not policy['group_chat_enabled'] or not policy['group_idle_enabled'] or not policy['idle_groups']:
            return
        platform = self.context.get_platform_inst(self.config.get('adapter_id','azusa-onebot'))
        if platform is None or platform.meta().name != 'aiocqhttp':
            return
        bot = platform.get_client()
        if policy['self_id'] not in getattr(bot, '_wsr_api_clients', {}):
            return
        base, headers = self._endpoint(), {'Authorization':'Bearer '+self._token()}
        response = await self.client.post(base+'/bridge/idle/claim', headers=headers,
            json={'self_id':policy['self_id']}, timeout=240)
        response.raise_for_status()
        items = response.json().get('messages')
        if not isinstance(items,list) or len(items)>1:
            raise ValueError('Invalid idle batch')
        for item in items:
            if (not isinstance(item,dict) or not re.fullmatch(r'[a-f0-9]{32}',str(item.get('id','')))
                    or not re.fullmatch(r'[a-f0-9]{32}',str(item.get('claim_token','')))):
                raise ValueError('Invalid idle identity')
            ack = {key:item[key] for key in ('id','claim_token')}
            ack.update(self_id=policy['self_id'],status='failed')
            try:
                current = self._policy()
                target = numeric_id(item.get('target_id'))
                text = item.get('text')
                if (item.get('self_id') != policy['self_id'] or not current['enabled']
                        or not current['group_chat_enabled'] or not current['group_idle_enabled'] or target not in current['idle_groups']
                        or current['self_id'] != policy['self_id']
                        or not self._scope_allowed(current,'group',target)):
                    continue
                if not isinstance(text,str) or not text.strip() or len(text)>600:
                    raise ValueError('Invalid idle text')
                authorization = await self.client.post(base+'/bridge/idle/authorize',headers=headers,
                    json={**{key:ack[key] for key in ('id','claim_token','self_id')}},timeout=10)
                authorization.raise_for_status()
                if authorization.json().get('allowed') is not True:
                    continue
                current = self._policy()
                if (not current['enabled'] or not current['group_chat_enabled'] or not current['group_idle_enabled']
                        or target not in current['idle_groups'] or current['self_id'] != item['self_id'] or not self._scope_allowed(current,'group',target)):
                    continue
                ack['status']='uncertain'
                receipt = await bot.send_group_msg(group_id=int(target), self_id=int(item['self_id']),
                    message=[{'type':'text','data':{'text':text}}])
                if not isinstance(receipt,dict):
                    raise ValueError('Missing idle receipt')
                ack.update(status='sent',message_id=message_id(receipt.get('message_id')))
            except Exception:
                logger.warning('XZBot idle participation send needs local review')
            finally:
                try:
                    result=await self.client.post(base+'/bridge/idle/ack',headers=headers,json=ack,timeout=10)
                    result.raise_for_status()
                except Exception:
                    logger.warning('XZBot idle participation receipt unavailable')

    async def _notification_tick(self):
        policy = self._policy()
        if not policy["enabled"]:
            return
        platform = self.context.get_platform_inst(self.config.get("adapter_id","azusa-onebot"))
        if platform is None or platform.meta().name != "aiocqhttp":
            return
        bot = platform.get_client()
        # The pinned aiocqhttp reverse-WS adapter keeps active account transports
        # here. Do not claim a durable notification while its QQ is offline.
        if policy["self_id"] not in getattr(bot,"_wsr_api_clients",{}):
            return
        base,headers = self._endpoint(),{"Authorization":"Bearer "+self._token()}
        response = await self.client.post(base+"/bridge/notifications/claim",headers=headers,
            json={"self_id":policy["self_id"]},timeout=10)
        response.raise_for_status()
        data=response.json()
        items=data.get("notifications") if isinstance(data,dict) else None
        if not isinstance(items,list) or len(items)>1:
            raise ValueError("Invalid notification batch")
        for item in items:
            if (not isinstance(item,dict) or not re.fullmatch(r"[a-f0-9]{32}",str(item.get("id","")))
                or not re.fullmatch(r"[a-f0-9]{32}",str(item.get("claim_token","")))):
                raise ValueError("Invalid notification identity")
            ack={"id":item["id"],"claim_token":item["claim_token"],"status":"failed"}
            try:
                current=self._policy()
                kind=item.get("kind")
                target=numeric_id(item.get("target_id"))
                if (item.get("transport")!="onebot" or item.get("self_id")!=policy["self_id"]
                    or not current["enabled"] or current["self_id"]!=policy["self_id"]
                    or kind not in {"group","dm"} or not self._scope_allowed(current,kind,target)
                    or kind=="dm" and target==current["self_id"]):
                    continue
                text=item.get("text")
                if not isinstance(text,str) or not text.strip() or len(text)>1500:
                    raise ValueError("Invalid notification text")
                segments = self._notification_segments(text, item.get("image_file"))
                authorization=await self.client.post(base+"/bridge/notifications/authorize",headers=headers,
                    json={"id":item["id"],"claim_token":item["claim_token"],"self_id":current["self_id"]},timeout=10)
                authorization.raise_for_status()
                if authorization.json().get("allowed") is not True:
                    continue
                # No await between the last local policy check and initiating the
                # sole send. Plain text segments cannot inject CQ @all markup.
                current=self._policy()
                if (not current["enabled"] or current["self_id"]!=item["self_id"]
                    or not self._scope_allowed(current,kind,target)):
                    continue
                ack["status"]="uncertain"
                outgoing={"self_id":int(item["self_id"]),"message":segments}
                if kind=="group":
                    receipt=await bot.send_group_msg(group_id=int(target),**outgoing)
                else:
                    receipt=await bot.send_private_msg(user_id=int(target),**outgoing)
                if not isinstance(receipt,dict):
                    raise ValueError("Missing notification receipt")
                message_id(receipt.get("message_id"))
                ack["status"]="sent"
            except Exception:
                logger.warning("XZBot subscription send needs local review")
            finally:
                try:
                    response=await self.client.post(base+"/bridge/notifications/ack",headers=headers,json=ack,timeout=10)
                    response.raise_for_status()
                except Exception:
                    logger.warning("XZBot subscription receipt unavailable")

    @staticmethod
    def _notification_segments(text, image_file):
        """Prepare a single text/image message before send; no remote URL fetches."""
        segments = [{"type": "text", "data": {"text": text}}]
        if not image_file:
            return segments
        try:
            configured = os.environ.get("AZUSA_MEDIA_ROOT", "")
            if not configured or not isinstance(image_file, str):
                raise ValueError()
            root = Path(configured).resolve(strict=True)
            path = Path(image_file)
            if (not path.is_absolute() or path.is_symlink()
                or not path.resolve(strict=True).is_relative_to(root)
                or not re.fullmatch(r"[a-f0-9]{32}\.png", path.name)):
                raise ValueError()
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 8 * 1024 * 1024:
                    raise ValueError()
                data = stream.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError()
            segments.append({"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")}})
        except (OSError, ValueError, TypeError):
            # Media failure is known BEFORE any send. Do not attempt an image
            # first and then send text after an uncertain API result.
            logger.warning("XZBot notification image unavailable; using text before send")
        return segments

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
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Core must be a loopback HTTP endpoint")
        return base

    def _policy(self):
        """Read the GUI's private policy on every event, with no cached fallback."""
        filename = os.environ.get("AZUSA_ONEBOT_POLICY_FILE", "")
        if not filename:
            policy = {"transport": "onebot", "onebot_enabled": self.config.get("enabled", False),
                      "onebot_self_id": self.config.get("self_id", ""),
                      "onebot_allowed_groups": self.config.get("allowed_groups", []),
                      "onebot_allowed_dm_users": self.config.get("allowed_dm_users", []),
                      "group_chat_enabled": self.config.get("group_chat_enabled", False),
                      "group_idle_enabled": self.config.get("group_idle_enabled", False),
                      "group_idle_groups": self.config.get("group_idle_groups", [])}
        else:
            path = Path(filename)
            if not path.is_absolute():
                raise ValueError("Policy path must be absolute")
            # O_NOFOLLOW plus fstat checks the opened file, avoiding a symlink or
            # permission-check/read race. Atomic GUI writes are safe to refresh.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                    or info.st_uid != os.geteuid() or info.st_size > 250000):
                    raise ValueError("Private owner-controlled policy file required")
                policy = json.load(stream)
        if not isinstance(policy, dict):
            raise ValueError("Policy must be an object")
        result = {"enabled": False, "self_id": "", "groups": frozenset(),
                  "dm_users": frozenset(), "group_chat_enabled": False, "group_idle_enabled": False, "idle_groups":frozenset()}
        if filename and not activity_allowed(Path(filename).parent.parent):
            return result
        if policy.get("transport") != "onebot" or policy.get("onebot_enabled") is not True:
            return result
        for source, target in (("onebot_allowed_groups", "groups"),
                               ("onebot_allowed_dm_users", "dm_users"), ("group_idle_groups", "idle_groups")):
            values = policy.get(source, [])
            if not isinstance(values, list) or len(values) > 10000:
                raise ValueError("Numeric identity allowlist required")
            result[target] = frozenset(numeric_id(value) for value in values)
        result.update(enabled=True, self_id=numeric_id(policy.get("onebot_self_id")),
                      group_chat_enabled=policy.get("group_chat_enabled") is True,
                      group_idle_enabled=policy.get("group_idle_enabled") is True)
        overrides={}
        if filename:
            groups_path=Path(filename).with_name("groups.json")
            try:
                descriptor=os.open(groups_path,os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                pass
            else:
                with os.fdopen(descriptor,"r") as stream:
                    info=os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)&0o077
                        or info.st_uid!=os.geteuid() or info.st_size>1024*1024):
                        raise ValueError("Private group policy required")
                    overrides=json.load(stream)
                if not isinstance(overrides,dict):raise ValueError("Invalid group policy")
        chat_groups=set();idle_groups=set()
        for group in result["groups"]:
            selected=overrides.get("onebot:"+result["self_id"]+":group:"+group,{})
            if not isinstance(selected,dict):raise ValueError("Invalid group policy")
            chat=selected.get("group_chat_enabled",result["group_chat_enabled"]) is True
            idle=selected.get("group_idle_enabled",result["group_idle_enabled"] and group in result["idle_groups"]) is True
            if chat:chat_groups.add(group)
            if chat and idle:idle_groups.add(group)
        result.update(chat_groups=frozenset(chat_groups),group_chat_enabled=bool(chat_groups),
                      idle_groups=frozenset(idle_groups),group_idle_enabled=bool(idle_groups))
        return result

    @staticmethod
    def _scope_allowed(policy, kind, scope_id):
        return scope_id in policy["groups" if kind == "group" else "dm_users"]

    @staticmethod
    def _display_name(sender):
        # QQ nicknames/cards are user-controlled labels, never authorization IDs.
        for key in ("card", "nickname"):
            value = sender.get(key)
            if isinstance(value, str):
                value = " ".join(value.split())
                value = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
                if value:
                    return value[:80]
        return ""

    @staticmethod
    def _text(segments, self_id):
        if not isinstance(segments, list) or len(segments) > 200:
            raise ValueError("OneBot array message required")
        parts, mentioned, reply_to_id, has_text = [], False, None, False
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                raise ValueError("Malformed message segment")
            kind, data = segment.get("type"), segment["data"]
            if kind == "text":
                if not isinstance(data.get("text"), str):
                    raise ValueError("Malformed text segment")
                # Literal CQ markup stays ordinary text, never a trusted @ segment.
                parts.append(data["text"])
                has_text = has_text or bool(data["text"].strip())
            elif kind == "at":
                target = data.get("qq")
                if target == "all":
                    parts.append(" @全体成员 ")
                elif numeric_id(target) == self_id:
                    mentioned = True
                else:
                    parts.append(" @" + numeric_id(target) + " ")
            elif kind == "reply":
                reference = message_id(data.get("id"))
                if reply_to_id is not None and reference != reply_to_id:
                    raise ValueError("Ambiguous reply reference")
                reply_to_id = reference
            # Image bytes travel separately; media/face/forward segments never
            # invent text from placeholders, URLs or quoted message bodies.
        text = "".join(parts).strip() if has_text else ""
        if len(text) > 10000:
            raise ValueError("Message too long")
        return text, mentioned, reply_to_id

    def _payload(self, event, policy=None):
        raw = event.message_obj.raw_message
        if not isinstance(raw, Event):
            raise ValueError("Trusted OneBot Event required")
        # Outgoing echoes, notices, requests, and anonymous/temporary messages do
        # not become a conversational identity or accidentally trigger replies.
        if raw.get("post_type") != "message":
            return None
        policy = self._policy() if policy is None else policy
        if not policy["enabled"]:
            return None
        self_id = numeric_id(raw.get("self_id"))
        if self_id != policy["self_id"]:
            raise ValueError("OneBot account mismatch")
        user_id = numeric_id(raw.get("user_id"))
        if user_id == self_id:
            return None
        sender = raw.get("sender")
        if not isinstance(sender, dict) or numeric_id(sender.get("user_id")) != user_id:
            raise ValueError("OneBot sender mismatch")
        if raw.get("message_type") == "group":
            if raw.get("sub_type") != "normal" or raw.get("anonymous") is not None:
                return None
            kind, scope_id = "group", numeric_id(raw.get("group_id"))
        elif raw.get("message_type") == "private":
            if raw.get("sub_type") != "friend" or raw.get("temp_source") is not None:
                return None
            kind, scope_id = "dm", user_id
        else:
            return None
        # Refuse unrelated scopes before extracting or forwarding their content.
        # This covers direct commands and @ messages as well as passive context.
        if not self._scope_allowed(policy, kind, scope_id):
            return None
        text, mentioned, reply_to_id = self._text(raw.get("message"), self_id)
        # Only native image segments can supply visual inputs. No file path,
        # quoted body, literal CQ string or ordinary text URL becomes an image.
        # The core validates QQ CDN hosts and downloads after its own gates.
        images = []
        for segment in raw["message"]:
            if segment.get("type") != "image":
                continue
            url = segment["data"].get("url")
            if isinstance(url, str) and 0 < len(url) <= 8192 and url not in images:
                images.append(url)
                if len(images) == 3:
                    break
        # Scan the already validated SDK segments separately. Literal @/CQ text
        # never proves that a continuation was addressed to another participant.
        other_mentioned = any(
            segment.get("type") == "at" and (
                segment["data"].get("qq") == "all"
                or numeric_id(segment["data"].get("qq")) != self_id
            ) for segment in raw["message"]
        )
        command = text.split(maxsplit=1)[0] if text else ""
        # Forward eligible white-listed text/images to the local core even while
        # proactive participation is off. The core independently applies the
        # current profile's group-memory policy. Forwarding does not authorize
        # a cloud request or a QQ reply; passive sends are gated again below.
        if not text and not images and kind != 'group':
            return None
        return {"transport": "onebot", "app_id": self_id, "kind": kind,
                "scope_id": scope_id, "user_id": user_id,
                "event_id": message_id(raw.get("message_id")), "text": text,
                "images": images,
                **({"activity_only": True} if not text and not images else {}),
                "mentioned": mentioned, "other_mentioned": other_mentioned,
                "display_name": self._display_name(sender),
                "reply_to_id": reply_to_id,
                "unprompted": kind == "group" and not mentioned and command not in COMMANDS}

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

    def _reply_allowed(self, payload):
        current = self._policy()
        return (current["enabled"] and current["self_id"] == payload["app_id"]
                and self._scope_allowed(current, payload["kind"], payload["scope_id"])
                and (not payload["unprompted"] or payload["scope_id"] in current.get("chat_groups", ())))

    async def _send_segments(self, event, payload, reply, base, headers):
        parts, interval = reply.get("segments"), reply.get("segment_interval_seconds")
        if (reply.get("mode") != "text" or not isinstance(parts, list) or not 2 <= len(parts) <= 3
                or any(not isinstance(text, str) or not text.strip() for text in parts)
                or sum(map(len, parts)) > 3000 or type(interval) not in (int, float) or not 0 <= interval <= 5
                or re.sub(r"\s", "", "".join(parts)) != re.sub(r"\s", "", reply.get("text", ""))):
            raise ValueError("Invalid segmented reply")
        for index, text in enumerate(parts):
            if index:
                await asyncio.sleep(interval)
            if not self._reply_allowed(payload):
                return
            outgoing = await AiocqhttpMessageEvent._parse_onebot_json(self._message({"mode":"text", "text":text}))
            if len(outgoing) != 1 or outgoing[0].get("type") != "text":
                raise ValueError("Invalid text segment")
            identity = {"reply_id":reply["reply_id"], "segment_index":index}
            auth = await self.client.post(base+"/bridge/reply/segment-authorize", headers=headers, json=identity, timeout=10)
            auth.raise_for_status()
            if auth.json().get("allowed") is not True or not self._reply_allowed(payload):
                return
            ack = dict(identity, status="uncertain")
            try:
                route = {"self_id":int(payload["app_id"]), "message":outgoing}
                if payload["kind"] == "group":
                    receipt = await event.bot.send_group_msg(group_id=int(payload["scope_id"]), **route)
                else:
                    receipt = await event.bot.send_private_msg(user_id=int(payload["user_id"]), **route)
                if not isinstance(receipt, dict):
                    raise ValueError("Missing segment receipt")
                ack.update(status="sent", message_id=message_id(receipt.get("message_id")))
            finally:
                response = await self.client.post(base+"/bridge/reply/segment-ack", headers=headers, json=ack, timeout=10)
                response.raise_for_status()
            # An exception, lost receipt or lost core ACK ends the batch. No retry.

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize)
    async def on_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        if event.get_platform_id() != self.config.get("adapter_id", "azusa-onebot"):
            return
        # The selected adapter belongs exclusively to this bridge, even disabled.
        event.should_call_llm(False)
        event.stop_event()
        reply_id, ack_status, delivered_message_id = None, "uncertain", None
        segmented = False
        try:
            policy = self._policy()
            if not policy["enabled"]:
                return
            payload = self._payload(event, policy)
            if payload is None:
                return
            base = self._endpoint()
            headers = {"Authorization": "Bearer " + self._token()}
            response = await self.client.post(base + "/bridge/message", json=payload, headers=headers)
            response.raise_for_status()
            reply = response.json()
            if not isinstance(reply, dict):
                raise ValueError("Invalid core reply")
            # Silent/ignored/rate-limited outcomes belong to the core, including
            # responses with a ledger reply_id. No outbound attempt means no ACK.
            if payload.get("activity_only") or reply.get("mode") == "none":
                return
            reply_id = reply.get("reply_id")
            if not isinstance(reply_id, str) or not reply_id:
                raise ValueError("Missing reply identity")
            delivery_key = (payload["app_id"], payload["kind"], payload["scope_id"])
            async with self._delivery_locks[hash(delivery_key) % len(self._delivery_locks)]:
                if "segments" in reply:
                    segmented = True
                    await self._send_segments(event, payload, reply, base, headers)
                    return
                message = self._message(reply)
                if message is not None:
                    # Generation/debounce may await other events. Recheck any GUI
                    # revocation before the sole outbound API call; never retry it.
                    current = self._policy()
                    if (not current["enabled"] or current["self_id"] != payload["app_id"]
                        or not self._scope_allowed(current, payload["kind"], payload["scope_id"])
                        or (payload["unprompted"] and payload["scope_id"] not in current.get("chat_groups",()))):
                        return
                    # Pinned AstrBot helper encodes local WAV/image data as base64.
                    # NapCat performs its own audio conversion. Keep the API receipt,
                    # unlike the upstream send() helper which discards it.
                    outgoing = await AiocqhttpMessageEvent._parse_onebot_json(message)
                    if len(outgoing) != 1 or outgoing[0].get("type") not in {"text", "record", "image"}:
                        raise ValueError("Exactly one outgoing segment required")
                    # A global role switch can occur during generation or media
                    # conversion. Authorize the pinned role immediately before send.
                    authorization = await self.client.post(base + "/bridge/reply/authorize",
                        headers=headers, json={"reply_id": reply_id}, timeout=10)
                    authorization.raise_for_status()
                    allowed = authorization.json()
                    if not isinstance(allowed, dict) or allowed.get("allowed") is not True:
                        return
                    current = self._policy()
                    if (not current["enabled"] or current["self_id"] != payload["app_id"]
                        or not self._scope_allowed(current, payload["kind"], payload["scope_id"])
                        or (payload["unprompted"] and payload["scope_id"] not in current.get("chat_groups",()))):
                        return
                    route = {"self_id": int(payload["app_id"]), "message": outgoing}
                    if payload["kind"] == "group":
                        receipt = await event.bot.send_group_msg(group_id=int(payload["scope_id"]), **route)
                    else:
                        receipt = await event.bot.send_private_msg(user_id=int(payload["user_id"]), **route)
                    if not isinstance(receipt, dict):
                        raise ValueError("OneBot delivery receipt unavailable")
                    delivered_message_id = message_id(receipt.get("message_id"))
                ack_status = "sent"
        except Exception:
            # Never log request/response bodies, member IDs, tokens, or exceptions.
            # A timeout/missing receipt must not trigger a retry or text fallback.
            logger.warning("XZBot OneBot bridge failed; delivery requires local review")
        finally:
            if reply_id:
                try:
                    if segmented:
                        response = await self.client.post(base+"/bridge/reply/segment-finish", headers=headers,
                                                          json={"reply_id":reply_id}, timeout=10)
                    else:
                        acknowledgement = {"reply_id": reply_id, "status": ack_status}
                        if ack_status == "sent" and delivered_message_id is not None:
                            acknowledgement["message_id"] = delivered_message_id
                        response = await self.client.post(base + "/bridge/ack", headers=headers,
                            json=acknowledgement)
                    response.raise_for_status()
                except Exception:
                    logger.warning("XZBot OneBot delivery acknowledgement unavailable")
            event.stop_event()
