"""Bounded platform reads. No user URL fetches, login automation or AI calls."""
from __future__ import annotations
import json
import asyncio
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import httpx
from bs4 import BeautifulSoup
from .credential_store import CredentialStoreError
from .subscription_credentials import credential_values

DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
STEAM_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
WEIBO_URL = "https://m.weibo.cn/api/container/getIndex"
WEIBO_CONFIG_URL = "https://m.weibo.cn/api/config"
TYPES = {"bilibili_live", "bilibili_dynamic", "bilibili_video", "steam", "weibo"}
INTERVALS = {"bilibili_live": 90, "bilibili_dynamic": 300, "bilibili_video": 300, "steam": 90, "weibo": 300}
# A space feed also carries live recommendations, ads and invalid entries.
# Only these post types have a supported ordinary-dynamic rendering contract.
_BILI_POST_TYPES = {"DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_FORWARD",
                    "DYNAMIC_TYPE_AV", "DYNAMIC_TYPE_ARTICLE"}


class SubscriptionError(RuntimeError):
    """Only fixed, non-sensitive codes cross platform boundaries."""

    def __init__(self, code, *, retry_at=0, retry_after_seconds=0):
        super().__init__(code)
        self.retry_at = float(retry_at)
        self.retry_after_seconds = max(0, int(retry_after_seconds))

    def public_details(self):
        result = {"detail": str(self)}
        if self.retry_after_seconds:
            result.update(retry_at=self.retry_at, retry_after_seconds=self.retry_after_seconds)
        return result


def label(value, limit=160):
    return " ".join(str(value or "").split())[:limit]


def _published_timestamp(value):
    """The dynamic feed serializes pub_ts as either an integer or decimal text.

    Reject booleans/floats and malformed strings rather than coercing an
    unknown publication time to now (which would replay an old post).
    """
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 253402300799:
        raise ValueError()
    return value


def source_id(kind, value):
    if not isinstance(value, str) or not 1 <= len(value) <= 300:
        raise ValueError("subscription_source_invalid")
    value = value.strip()
    # Do not hard-code the historical 7656119 prefix: newer public account
    # IDs can begin 7656120. Official response identity is checked below.
    pattern = r"[1-9][0-9]{16}" if kind == "steam" else r"[1-9][0-9]{0,19}"
    if re.fullmatch(pattern, value):
        return value
    try:
        url = urlsplit(value)
        hosts = {"steamcommunity.com"} if kind == "steam" else {"space.bilibili.com"}
        prefix = r"/profiles/" if kind == "steam" else r"/"
        if kind == "weibo":
            hosts = {"weibo.com", "www.weibo.com", "m.weibo.cn"}
            prefix = r"/(?:u|profile)/" if url.hostname == "m.weibo.cn" else r"/u/"
        if (url.scheme not in {"http", "https"} or url.hostname not in hosts or url.username
                or url.password or url.port is not None or kind == "weibo" and (url.query or url.fragment)
                or not re.fullmatch(prefix + pattern + r"/?", url.path)):
            raise ValueError()
        return url.path.rstrip("/").split("/")[-1]
    except ValueError:
        raise ValueError("subscription_source_requires_numeric_id_or_profile_url") from None


def _weibo_text(value, limit=600):
    """Feed markup is display data, never executable HTML or a fetched link."""
    if not isinstance(value, str):
        return ""
    soup = BeautifulSoup(value[:16000], "html.parser")
    for tag in soup(["script", "style", "iframe", "object", "template"]):
        tag.decompose()
    for tag in soup.find_all("img"):
        tag.replace_with(str(tag.get("alt", ""))[:40])
    return label(soup.get_text(" ", strip=True), limit)


def _card_text(value):
    """Keep source paragraphs, with a bounded display payload and no rewriting."""
    if not isinstance(value, str):
        return "", False
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in value.split("\n"))
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value[:5000], len(value) > 5000


def _image_addresses(values):
    # Source data is not a fetch instruction. The renderer separately enforces
    # its CDN allowlist, request limits and credential-free image transport.
    result, seen = [], set()
    for value in values[:100]:
        if isinstance(value, str) and 0 < len(value) <= 2048 and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _card_blocks(blocks):
    remaining = 9
    for block in blocks:
        images = block["images"]
        block["images_total"] = max(block.get("images_total", 0), len(images))
        block["images"] = images[:remaining]
        remaining -= len(block["images"])
    return {"blocks": blocks}


def _dynamic_block(item, *, forwarded=False):
    modules = item.get("modules")
    modules = modules if isinstance(modules, dict) else {}
    author = modules.get("module_author")
    author = author if isinstance(author, dict) else {}
    content = modules.get("module_dynamic")
    content = content if isinstance(content, dict) else {}
    major = content.get("major")
    major = major if isinstance(major, dict) else {}
    desc = content.get("desc")
    desc = desc.get("text", "") if isinstance(desc, dict) else ""
    title, images, preview_only = "", [], False
    if isinstance(major.get("archive"), dict):
        title = major["archive"].get("title", "")
        images = [major["archive"].get("cover", "")]
    elif isinstance(major.get("opus"), dict):
        opus = major["opus"]
        title = opus.get("title", "")
        summary = opus.get("summary")
        desc = desc or (summary.get("text", "") if isinstance(summary, dict) else "")
        pics = opus.get("pics")
        images = [p.get("url", "") for p in pics if isinstance(p, dict)] if isinstance(pics, list) else []
    elif isinstance(major.get("draw"), dict):
        pics = major["draw"].get("items")
        images = [p.get("src", "") for p in pics if isinstance(p, dict)] if isinstance(pics, list) else []
    elif isinstance(major.get("article"), dict):
        title = major["article"].get("title", "")
        covers = major["article"].get("covers")
        images = covers if isinstance(covers, list) else []
        # The feed includes a title/preview, not the linked article body.
        preview_only = True
    text, truncated = _card_text(desc)
    title, title_truncated = _card_text(title)
    # Titles are separate content, not replacements for a video's caption.
    title_truncated = title_truncated or len(title) > 300
    title = title[:300]
    images = _image_addresses(images)
    if title == text:
        title = ""
    block_label = ""
    if forwarded:
        name = label(author.get("name"), 80)
        block_label = "转发原文" + (" · " + name if name else "")
        if not (title or text or images):
            text = "原动态已删除或平台未返回可见内容。"
            truncated = True
    return {"label": block_label, "title": title, "text": text, "images": images,
            "truncated": truncated or title_truncated or preview_only
                or forwarded and ("orig" in item or item.get("type") == "DYNAMIC_TYPE_FORWARD")}


def _dynamic_card(item):
    blocks = [_dynamic_block(item)]
    original = item.get("orig")
    if isinstance(original, dict):
        blocks.append(_dynamic_block(original, forwarded=True))
    elif item.get("type") == "DYNAMIC_TYPE_FORWARD":
        blocks.append(_dynamic_block({}, forwarded=True))
    return _card_blocks(blocks)


def _weibo_card_text(value):
    if not isinstance(value, str):
        return "", False
    soup = BeautifulSoup(value[:32000], "html.parser")
    for tag in soup(["script", "style", "iframe", "object", "template"]):
        tag.decompose()
    for tag in soup.find_all("img"):
        tag.replace_with(str(tag.get("alt", ""))[:40])
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all(["p", "div"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text, truncated = _card_text(soup.get_text())
    return text, truncated or len(value) > 32000


def _weibo_block(post, *, forwarded=False):
    long_text = post.get("longText")
    long_text = long_text.get("longTextContent") if isinstance(long_text, dict) else None
    long_text = long_text or post.get("longTextContent")
    complete = isinstance(long_text, str) and bool(long_text.strip())
    raw = long_text if complete else post.get("text", "")
    text, truncated = _weibo_card_text(raw)
    truncated = truncated or bool(post.get("isLongText") and not complete)
    pics = post.get("pics")
    # RSSHub's Weibo adapter documents both list and indexed-object shapes.
    # Keep the platform's insertion order; do not sort arbitrary object keys.
    if isinstance(pics, dict):
        pics = list(pics.values())
    images = []
    if isinstance(pics, list):
        for picture in pics:
            if isinstance(picture, dict):
                large = picture.get("large")
                images.append((large.get("url") if isinstance(large, dict) else "") or picture.get("url", ""))
    images = _image_addresses(images)
    total = post.get("pic_num")
    total = total if type(total) is int and 0 <= total <= 1000 else len(images)
    if not images:
        page = post.get("page_info")
        picture = page.get("page_pic") if isinstance(page, dict) else None
        images = _image_addresses([picture.get("url")]) if isinstance(picture, dict) else []
    block_label = ""
    if forwarded:
        user = post.get("user")
        name = _weibo_text(user.get("screen_name"), 80) if isinstance(user, dict) else ""
        block_label = "转发原文" + (" · " + name if name else "")
        if not (text or images):
            text = "原微博已删除或平台未返回可见内容。"
            truncated = True
    return {"label": block_label, "title": "", "text": text, "images": images,
            "truncated": truncated or forwarded and "retweeted_status" in post, "images_total": total}


def _weibo_card(post):
    blocks = [_weibo_block(post)]
    original = post.get("retweeted_status")
    if isinstance(original, dict):
        blocks.append(_weibo_block(original, forwarded=True))
    elif "retweeted_status" in post:
        blocks.append(_weibo_block({}, forwarded=True))
    return _card_blocks(blocks)


def _weibo_time(value, now):
    if not isinstance(value, str):
        raise ValueError()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is not None:
            return int(parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        pass
    local = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
    if value == "刚刚":
        return int(now)
    match = re.fullmatch(r"([0-9]{1,3})(分钟|小时)前", value)
    if match:
        return int(now) - int(match[1]) * (60 if match[2] == "分钟" else 3600)
    # Mobile cards also use local wall time. In particular, a pinned post
    # labelled "昨天 HH:MM" must not invalidate all newer posts in the feed.
    match = re.fullmatch(r"(今天|昨天) ([0-9]{2}):([0-9]{2})", value)
    if match:
        parsed = local.replace(hour=int(match[2]), minute=int(match[3]), second=0, microsecond=0)
        if match[1] == "昨天":
            parsed -= timedelta(days=1)
        return int(parsed.timestamp())
    # Date-only values are historical, and deliberately cannot imply a new
    # event within the fifteen-minute push window.
    for form in ("%Y-%m-%d %H:%M", "%m-%d %H:%M", "%Y-%m-%d", "%m-%d"):
        try:
            parsed = datetime.strptime(value, form).replace(tzinfo=local.tzinfo)
            if form.startswith("%m-%d"):
                parsed = parsed.replace(year=local.year)
                if parsed > local:
                    parsed = parsed.replace(year=local.year-1)
            return int(parsed.timestamp())
        except ValueError:
            pass
    raise ValueError()


class Sources:
    def __init__(self, root, client=None, *, clock=time.time):
        self.root, self.client, self.clock = root, client, clock
        self._weibo_profiles = {}
        from .subscription_cooldown import SourceCooldown
        self.cooldown = SourceCooldown(root, clock=clock)

    async def credentials(self, names):
        try:
            return await asyncio.to_thread(credential_values,self.root,names)
        except CredentialStoreError:
            raise SubscriptionError("subscription_credentials_unavailable") from None

    def _retry_after(self, value):
        """Honor a bounded upstream pause without storing its response/header."""
        if not isinstance(value, str) or len(value) > 100:
            return 0
        if re.fullmatch(r"[0-9]{1,10}", value):
            return min(86400, int(value))
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is not None:
                return min(86400, max(0, int(stamp.timestamp()-self.clock()+.999)))
        except (ValueError, TypeError, OverflowError):
            pass
        return 0

    async def request(self, url, params, headers, prefix):
        if url not in {DYNAMIC_URL, STEAM_URL, WEIBO_URL, WEIBO_CONFIG_URL}:
            raise ValueError("subscription_endpoint_invalid")
        async def fetch(client):
            try:
                async with client.stream("GET", url, params=params, headers=headers, follow_redirects=False) as response:
                    if response.status_code != 200:
                        if prefix == "bilibili_dynamic" and response.status_code == 412:
                            raise SubscriptionError("bilibili_dynamic_access_rejected")
                        if prefix == "bilibili_dynamic" and response.status_code == 429:
                            raise SubscriptionError("bilibili_dynamic_rate_limited")
                        if prefix == "weibo":
                            # RSSHub treats 432 and JSON ok=-100 as an expired
                            # visitor/login session, not a malformed post.
                            if response.status_code in {301, 302, 303, 307, 308, 401, 432}:
                                raise SubscriptionError("weibo_login_required")
                            if response.status_code in {403, 418}:
                                raise SubscriptionError("weibo_access_rejected")
                            if response.status_code == 429:
                                raise SubscriptionError("weibo_rate_limited",
                                    retry_after_seconds=self._retry_after(response.headers.get("retry-after")))
                            if response.status_code == 503:
                                raise SubscriptionError("weibo_http_503",
                                    retry_after_seconds=self._retry_after(response.headers.get("retry-after")))
                        raise SubscriptionError(prefix + "_http_" + str(response.status_code))
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > 2 * 1024 * 1024:
                            raise SubscriptionError(prefix + "_response_too_large")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    if prefix == "weibo":
                        raise SubscriptionError("weibo_response_not_json") from None
                    raise
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except SubscriptionError:
                raise
            except httpx.HTTPError:
                if prefix == "weibo":
                    raise SubscriptionError("weibo_network_error") from None
                raise SubscriptionError(prefix + "_network_or_format") from None
            except (ValueError, UnicodeError, TypeError):
                raise SubscriptionError(prefix + "_network_or_format") from None
        async def limited(client):
            try:
                async with asyncio.timeout(20):
                    return await fetch(client)
            except TimeoutError:
                code = "weibo_network_error" if prefix == "weibo" else prefix + "_network_or_format"
                raise SubscriptionError(code) from None
        if self.client is not None:
            return await limited(self.client)
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), trust_env=False, follow_redirects=False) as client:
            return await limited(client)

    async def dynamic(self, uid):
        uid = source_id("bilibili_dynamic", uid)
        values=await self.credentials(("bilibili_sessdata","bilibili_bili_jct","bilibili_buvid3"))
        sessdata = values["bilibili_sessdata"]
        if not sessdata:
            raise SubscriptionError("bilibili_credentials_required")
        source = "bilibili_dynamic:" + uid
        self.cooldown.claim(source)
        try:
            result = await self._dynamic(uid, values)
        except SubscriptionError as exc:
            raise self.cooldown.failed(source, exc) from None
        except BaseException:
            self.cooldown.release(source)
            raise
        self.cooldown.success(source)
        return result

    async def _dynamic(self, uid, values):
        sessdata = values["bilibili_sessdata"]
        cookies = {"SESSDATA": sessdata, "bili_jct": values["bilibili_bili_jct"],
                   "buvid3": values["bilibili_buvid3"]}
        # Legacy DRAW responses can omit all text, even for text-only posts.
        # Request the opus representation whose summary carries the body.
        data = await self.request(DYNAMIC_URL, {"host_mid": uid, "offset": "", "timezone_offset": "-480",
            "platform": "web", "features": "itemOpusStyle"},
            {"Cookie": "; ".join(k + "=" + v for k, v in cookies.items() if v),
             "Referer": "https://space.bilibili.com/" + uid + "/dynamic",
             "Accept": "application/json, text/plain, */*",
             "User-Agent": "XZBot/0.4 (+private subscription reader)"}, "bilibili_dynamic")
        if type(data.get("code")) is int and data["code"] in {-352, -412}:
            raise SubscriptionError("bilibili_dynamic_access_rejected")
        if type(data.get("code")) is int and data["code"] in {-509, -799}:
            raise SubscriptionError("bilibili_dynamic_rate_limited")
        if type(data.get("code")) is int and data["code"] == -101:
            raise SubscriptionError("bilibili_dynamic_login_required")
        if type(data.get("code")) is not int or data["code"] != 0:
            raise SubscriptionError("bilibili_dynamic_auth_or_api_rejected")
        payload = data.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SubscriptionError("bilibili_dynamic_response_invalid")
        events, name, recognized = [], "B站用户 " + uid, 0
        try:
            for item in payload["items"][:50]:
                if not isinstance(item, dict):
                    raise ValueError()
                dynamic_id = str(item.get("id_str", ""))
                if not re.fullmatch(r"[1-9][0-9]{0,24}", dynamic_id):
                    continue
                modules = item.get("modules") or {}
                author = modules.get("module_author") or {}
                # Do not emit feed contamination, ads or another author's item.
                if str(author.get("mid", "")) != uid:
                    continue
                name = label(author.get("name"), 80) or name
                item_type = item.get("type")
                if item_type is not None and (not isinstance(item_type, str)
                        or not item_type.startswith("DYNAMIC_TYPE_")):
                    raise ValueError()
                recognized += 1
                if item_type is not None and item_type not in _BILI_POST_TYPES:
                    continue
                content = modules.get("module_dynamic") or {}
                major = content.get("major") or {}
                if major.get("type") in {"MAJOR_TYPE_BLOCKED", "MAJOR_TYPE_NONE"}:
                    continue
                # Missing type is tolerated for legacy feeds, but visible
                # source content is mandatory. Generated forward placeholders
                # do not count as content and must not create ghost posts.
                visible = [_dynamic_block(item)]
                if isinstance(item.get("orig"), dict):
                    visible.append(_dynamic_block(item["orig"]))
                if not any(b["title"] or b["text"] or b["images"] for b in visible):
                    # Fail the snapshot so polling retains its last baseline;
                    # a later complete response can still deliver this post.
                    raise SubscriptionError("bilibili_dynamic_content_unavailable")
                card = _dynamic_card(item)
                block = card["blocks"][0]
                title = block["title"]
                summary = label(block["text"] or title, 600) or (
                    f"发布了 {block['images_total']} 张图片" if block["images"] else "转发了一条动态")
                cover = next((b["images"][0] for b in card["blocks"] if b["images"]), "")
                url = "https://t.bilibili.com/" + dynamic_id
                published = _published_timestamp(author.get("pub_ts", 0))
                events.append({"session": dynamic_id, "title": label(title or summary), "summary": summary,
                    "text": f"{name}发动态啦：{summary}\n{url}", "url": url, "cover": label(cover, 2048),
                    "avatar": label(author.get("face"), 2048), "published_at": published,
                    "name": name, "card": card, "item_type": item_type,
                    "video": self._video_metadata(item_type, major)})
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            raise SubscriptionError("bilibili_dynamic_response_invalid") from None
        if payload["items"] and not recognized:
            # Nonempty but wholly unrecognizable/wrong-author data is not an
            # empty feed. Never establish an unreliable initial baseline.
            raise SubscriptionError("bilibili_dynamic_response_invalid")
        events.sort(key=lambda item: int(item["session"]))
        latest = events[-1] if events else {}
        return {"type": "bilibili_dynamic", "source_id": uid, "room_id": "dynamic:" + uid, "uid": uid,
            "name": name, "title": latest.get("title", "暂无可读的普通动态"), "live_status": 0,
            "session": latest.get("session", "0"), "url": "https://space.bilibili.com/" + uid + "/dynamic",
            "events": events, "cover": latest.get("cover", ""), "avatar": latest.get("avatar", ""),
            "card": latest.get("card", {"blocks": []})}

    @staticmethod
    def _video_metadata(item_type, major):
        archive = major.get("archive") if isinstance(major, dict) else None
        if item_type != "DYNAMIC_TYPE_AV" or not isinstance(archive, dict):return None
        bvid, title = archive.get("bvid"), archive.get("title")
        if not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or not isinstance(title, str) or not title.strip():return None
        return {"bvid": bvid, "title": label(title, 300), "description": label(archive.get("desc"), 2000),
                "url": "https://www.bilibili.com/video/" + bvid}

    @staticmethod
    def videos_from_dynamic(info):
        from copy import deepcopy
        result = deepcopy(info)
        events=[]
        for event in result.get("events", []):
            video=event.get("video")
            if event.get("item_type") != "DYNAMIC_TYPE_AV" or not isinstance(video, dict):continue
            event.update(title=video["title"], url=video["url"],
                         text=f"{info['name']}发布了视频：{video['title']}\n{video['url']}")
            events.append(event)
        latest=events[-1] if events else {}
        result.update(type="bilibili_video", room_id="video:"+info["uid"], events=events,
                      session=latest.get("session","0"), title=latest.get("title","暂无可读的投稿"),
                      url="https://space.bilibili.com/"+info["uid"]+"/video")
        return result

    async def video(self, uid):
        return self.videos_from_dynamic(await self.dynamic(uid))

    async def weibo(self, uid, *, now=None):
        """Read visible posts only; no visitor-login or challenge automation."""
        now = self.clock() if now is None else now
        uid = source_id("weibo", uid)
        values = await self.credentials(("weibo_sub", "weibo_cookie"))
        cookie = values.get("weibo_cookie") or ("SUB=" + values["weibo_sub"] if values.get("weibo_sub") else "")
        source = "weibo:" + uid
        from .weibo_session import require_usable_session
        require_usable_session(self,cookie)
        self.cooldown.claim(source)
        try:
            result = await self._weibo(uid, cookie, now)
        except SubscriptionError as exc:
            raise self.cooldown.failed(source, exc) from None
        except BaseException:
            self.cooldown.release(source)
            raise
        self.cooldown.success(source)
        return result

    @staticmethod
    def weibo_headers(cookie, uid=''):
        # Match the mobile API contract used by RSSHub's Weibo adapter.
        headers = {"Referer": "https://m.weibo.cn/" + ('u/'+uid if uid else ''),
                   "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 11_0 like Mac OS X) AppleWebKit/604.1.38 (KHTML, like Gecko) Version/11.0 Mobile/15A372 Safari/604.1",
                   "X-Requested-With": "XMLHttpRequest", "MWeibo-Pwa": "1",
                   "Accept": "application/json, text/plain, */*"}
        if cookie:headers["Cookie"] = cookie
        return headers

    async def _weibo(self, uid, cookie, now):
        headers = self.weibo_headers(cookie, uid)
        fingerprint = hashlib.sha256(cookie.encode()).hexdigest()
        cached = self._weibo_profiles.get(uid)
        if cached and cached[0] > self.clock() and cached[1] == fingerprint:
            _, _, container, name, avatar = cached
        else:
            profile = await self.request(WEIBO_URL, {"type": "uid", "value": uid}, headers, "weibo")
            self._weibo_result(profile)
            try:
                info = profile["data"]["userInfo"]
                if str(info["id"]) != uid:
                    raise ValueError()
                tabs = profile["data"]["tabsInfo"]["tabs"]
                container = next(str(tab["containerid"]) for tab in tabs if tab.get("tab_type") == "weibo")
                if not re.fullmatch(r"[0-9]{1,40}", container):
                    raise ValueError()
                name = _weibo_text(info.get("screen_name"), 80) or "微博用户 " + uid
                avatar = label(info.get("profile_image_url"), 2048)
            except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
                raise SubscriptionError("weibo_response_invalid") from None
            if uid not in self._weibo_profiles and len(self._weibo_profiles) >= 128:
                self._weibo_profiles.pop(next(iter(self._weibo_profiles)))
            self._weibo_profiles[uid] = (self.clock()+3600, fingerprint, container, name, avatar)
        feed = await self.request(WEIBO_URL, {"type": "uid", "value": uid, "containerid": container}, headers, "weibo")
        self._weibo_result(feed)
        try:
            cards = feed["data"]["cards"]
            if not isinstance(cards, list):
                raise ValueError()
            events, seen, queue = [], set(), list(cards[:50])
            inspected = 0
            while queue and inspected < 100:
                card = queue.pop(0); inspected += 1
                if not isinstance(card, dict):
                    raise ValueError()
                group = card.get("card_group", [])
                if isinstance(group, list):
                    queue.extend(group[:50])
                post = card.get("mblog")
                if not isinstance(post, dict) or str((post.get("user") or {}).get("id")) != uid:
                    continue
                post_id = str(post.get("idstr") or post.get("id", ""))
                if not re.fullmatch(r"[1-9][0-9]{0,24}", post_id) or post_id in seen:
                    continue
                seen.add(post_id)
                summary = _weibo_text(post.get("text")) or "发布了一条微博"
                original = post.get("retweeted_status")
                if isinstance(original, dict):
                    original_name = _weibo_text((original.get("user") or {}).get("screen_name"), 80)
                    original_text = _weibo_text(original.get("text"), 350)
                    summary = label(summary + " 转发 @" + original_name + "：" + original_text, 600)
                rich_card = _weibo_card(post)
                cover = next((b["images"][0] for b in rich_card["blocks"] if b["images"]), "")
                url = "https://m.weibo.cn/detail/" + post_id
                events.append({"session": post_id, "title": label(summary), "summary": summary,
                    "text": f"{name}发微博啦：{summary}\n{url}", "url": url,
                    "cover": label(cover, 2048), "avatar": avatar, "name": name, "card": rich_card,
                    "published_at": _weibo_time(post.get("created_at"), now)})
            if cards and not events:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise SubscriptionError("weibo_response_invalid") from None
        events.sort(key=lambda item: int(item["session"]))
        latest = events[-1] if events else {}
        return {"type": "weibo", "source_id": uid, "room_id": "weibo:" + uid, "uid": uid,
            "name": name, "title": latest.get("title", "暂无可见微博"), "live_status": 0,
            "session": latest.get("session", "0"), "url": "https://m.weibo.cn/u/" + uid,
            "events": events, "cover": latest.get("cover", ""), "avatar": avatar,
            "card": latest.get("card", {"blocks": []})}

    @staticmethod
    def _weibo_result(data):
        if type(data.get("ok")) is int and data["ok"] == -100:
            raise SubscriptionError("weibo_login_required")
        if type(data.get("ok")) is not int or data["ok"] != 1:
            raise SubscriptionError("weibo_auth_or_api_rejected")

    async def steam_many(self, ids):
        ids = [source_id("steam", value) for value in ids]
        if not ids or len(ids) > 100:
            raise ValueError("steam_batch_invalid")
        key = (await self.credentials(("steam_api_key",)))["steam_api_key"]
        if not key:
            raise SubscriptionError("steam_credentials_required")
        # Official Steam auth supports this header; the key never enters URLs/logs.
        data = await self.request(STEAM_URL, {"steamids": ",".join(ids)}, {"x-webapi-key": key}, "steam")
        response = data.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("players"), list):
            raise SubscriptionError("steam_response_invalid")
        result = {}
        try:
            for player in response["players"]:
                sid = str(player.get("steamid", ""))
                if sid not in ids:
                    continue
                if player.get("communityvisibilitystate") != 3:
                    continue
                state = player.get("personastate")
                if type(state) is not int or not 0 <= state <= 6:
                    continue
                game = str(player.get("gameid") or "")
                if game and not re.fullmatch(r"[1-9][0-9]{0,19}", game):
                    raise ValueError()
                name = label(player.get("personaname"), 80) or "Steam 好友"
                title = label(player.get("gameextrainfo")) or ("游戏 " + game if game else "在线" if state else "离线")
                session = json.dumps({"gameid": game, "online": bool(state)}, sort_keys=True, separators=(",", ":"))
                result[sid] = {"type": "steam", "source_id": sid, "room_id": "steam:" + sid, "uid": sid,
                    "name": name, "title": title, "live_status": int(bool(state)), "session": session,
                    "url": "https://steamcommunity.com/profiles/" + sid, "gameid": game,
                    "avatar": label(player.get("avatarfull") or player.get("avatarmedium"), 2048),
                    "cover": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/" + game + "/header.jpg" if game and len(game) < 11 else ""}
        except (ValueError, TypeError, AttributeError):
            raise SubscriptionError("steam_response_invalid") from None
        return result

    async def steam(self, sid):
        sid = source_id("steam", sid)
        result = await self.steam_many([sid])
        if sid not in result:
            raise SubscriptionError("steam_profile_private_or_unavailable")
        return result[sid]
