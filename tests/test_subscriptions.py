"""Offline integration of platform adapters, shared outbox, cards and migration."""
import asyncio
import io
import json
from pathlib import Path
import sqlite3

import httpx
from PIL import Image
import pytest

from azusa_bot.bilibili import BilibiliSubscriptions, BilibiliError
from azusa_bot.settings import Settings, write_settings, read_settings
from azusa_bot.subscription_credentials import credential_status, update_credentials
from azusa_bot.credential_store import CredentialStore, CredentialStoreError
from azusa_bot.subscription_cards import Cards, safe_image_url
from azusa_bot.subscription_sources import source_id

SID = "76561198000000001"


def dynamic(number, published=1000, uid=456, text="合成动态内容"):
    return {"id_str": str(number), "modules": {"module_author": {"mid": uid, "name": "测试UP", "pub_ts": int(published),
        "face": "https://i0.hdslb.com/bfs/face/avatar.png"}, "module_dynamic": {"desc": {"text": text},
        "major": {"opus": {"pics": [{"url": "https://i0.hdslb.com/bfs/new_dyn/cover.png"}]}}}}}


@pytest.fixture
def rig(tmp_path):
    now = [1000.0]; calls = []; failure = [None]
    feed = [dynamic(100)]
    players = [{"steamid": SID, "personaname": "测试好友", "personastate": 1, "communityvisibilitystate": 3,
                "avatarfull": "https://avatars.steamstatic.com/avatar.png"}]
    stream = io.BytesIO(); Image.new("RGB", (480, 270), "#669977").save(stream, format="PNG")
    image = stream.getvalue()
    def handler(request):
        calls.append(request)
        if request.url.host == "api.bilibili.com":
            assert "SESSDATA=test-session" in request.headers["cookie"]
            return httpx.Response(failure[0], text="private upstream error") if failure[0] else httpx.Response(200, json={"code": 0, "data": {"items": list(feed)}})
        if request.url.host == "api.steampowered.com":
            assert request.headers["x-webapi-key"] == "test-key"
            assert "key" not in request.url.params
            return httpx.Response(failure[0], text="private upstream error") if failure[0] else httpx.Response(200, json={"response": {"players": list(players)}})
        assert "cookie" not in request.headers and "x-webapi-key" not in request.headers
        return httpx.Response(200, content=image, headers={"content-type": "image/png"})
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id="10001", onebot_enabled=True,
        onebot_allowed_groups=["20001", "20002"], onebot_allowed_dm_users=["30001"]))
    update_credentials(tmp_path, {"bilibili_sessdata": "test-session", "steam_api_key": "test-key"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = BilibiliSubscriptions(tmp_path, client=client, clock=lambda: now[0])
    yield service, now, feed, players, calls, failure


def body(kind="bilibili_dynamic", **kw):
    return {"type": kind, "input_type": "steamid" if kind == "steam" else "uid", "value": SID if kind == "steam" else "456",
        "kind": "group", "target_id": "20001", "enabled": True, "include_image": False, **kw}


async def tick(rig, seconds=301):
    rig[1][0] += seconds
    return await rig[0].poll_once()


async def steam_change(rig, **change):
    rig[3][0].update(change)
    await tick(rig, 91); await tick(rig, 91)


async def test_dynamic_baseline_high_water_pinned_and_restart(rig):
    service, now, feed, *_ = rig
    sub = await service.add(body())
    assert sub["source_id"] == "456" and sub["type"] == "bilibili_dynamic"
    await tick(rig)
    assert service.fetch_claim("10001") == []
    feed[:] = [dynamic(99, now[0]), dynamic(101, now[0], text="新动态")]
    await tick(rig)
    message = service.fetch_claim("10001")[0]
    assert "新动态" in message["text"] and message["session"] == "101"
    assert service.ack(message["id"], message["claim_token"], "sent")["acknowledged"]
    restarted = BilibiliSubscriptions(service.root, client=service.client, clock=lambda: now[0])
    now[0] += 301; await restarted.poll_once()
    assert restarted.fetch_claim("10001") == []
    feed[:] = [dynamic(50, now[0])]
    await tick(rig)
    assert service.list()[0]["last_session"] == "101"


async def test_dynamic_only_recent_max_five_and_same_target_throttle(rig):
    service, now, feed, *_ = rig
    await service.add(body())
    feed[:] = [dynamic(n, now[0]) for n in range(101, 110)] + [dynamic(110, 1)]
    await tick(rig)
    assert service.status()["pending"] == 5
    first = service.fetch_claim("10001", 10); assert len(first) == 1
    service.ack(first[0]["id"], first[0]["claim_token"], "uncertain")
    assert service.fetch_claim("10001", 10) == []
    now[0] += 31
    assert len(service.fetch_claim("10001", 10)) == 1
    assert service.list()[0]["last_session"] == "110"


async def test_dynamic_multiple_targets_fetch_once_and_image_toggle_cancels_claim(rig):
    service, now, feed, _, calls, _ = rig
    first = await service.add(body())
    await service.add(body(target_id="20002", include_image=True))
    before = sum(r.url.host == "api.bilibili.com" for r in calls)
    feed.append(dynamic(101, now[0])); await tick(rig)
    assert sum(r.url.host == "api.bilibili.com" for r in calls) == before + 1
    items = service.fetch_claim("10001"); assert len(items) == 2
    plain = next(item for item in items if item["target_id"] == "20001")
    card = next(item for item in items if item["target_id"] == "20002")
    assert plain["image_file"] == ""
    assert Path(card["image_file"]).parent == service.root / "data/media"
    with Image.open(card["image_file"]) as image:
        assert image.width == 960 and 440 <= image.height <= 10000
    await service.update(first["id"], {"include_image": True})
    assert not service.authorize_claim(plain["id"], plain["claim_token"], "10001")


async def test_pause_dynamic_and_fresh_reenable_do_not_replay(rig):
    service, now, feed, *_ = rig
    sub = await service.add(body())
    feed.append(dynamic(101, now[0])); await tick(rig)
    item = service.fetch_claim("10001")[0]
    await service.update(sub["id"], {"enabled": False})
    assert not service.authorize_claim(item["id"], item["claim_token"], "10001")
    feed.append(dynamic(102, now[0])); await service.update(sub["id"], {"enabled": True})
    await tick(rig); assert service.fetch_claim("10001") == []


async def test_dynamic_failed_api_keeps_baseline_and_backoff(rig):
    service, now, feed, _, calls, failure = rig
    sub = await service.add(body())
    failure[0] = 412
    await tick(rig); before = len(calls)
    assert service.list()[0]["last_session"] == "100"
    assert service.status()["last_error"] == "bilibili_dynamic_access_rejected"
    await tick(rig, 15); assert len(calls) == before
    assert "private upstream" not in json.dumps(service.status())
    await service.update(sub["id"], {"enabled": False})
    with pytest.raises(BilibiliError): await service.update(sub["id"], {"enabled": True})
    assert not service.list()[0]["enabled"]


async def test_live_feed_card_never_advances_dynamic_baseline_or_enters_outbox(rig):
    service, now, feed, *_ = rig
    await service.add(body())
    live = dynamic(999, now[0]) | {"type": "DYNAMIC_TYPE_LIVE_RCMD"}
    live["modules"]["module_dynamic"] = {"major": {"type": "MAJOR_TYPE_LIVE_RCMD",
        "live_rcmd": {"content": '{"live_play_info":{"title":"合成直播"}}'}}}
    feed.extend([dynamic(101, now[0]), live])
    await tick(rig)
    with service.db() as db:
        assert [r[0] for r in db.execute("SELECT session FROM outbox")] == ["101"]
    assert service.list()[0]["last_session"] == "101"
    feed.append(dynamic(102, now[0]))
    await tick(rig)
    with service.db() as db:
        assert {r[0] for r in db.execute("SELECT session FROM outbox")} == {"101", "102"}
    assert service.list()[0]["last_session"] == "102"


async def test_blank_dynamic_keeps_baseline_until_content_recovers(rig):
    service, now, feed, *_ = rig
    await service.add(body())
    blank = dynamic(102, now[0], text="") | {"type": "DYNAMIC_TYPE_DRAW"}
    blank["modules"]["module_dynamic"]["major"] = {"type": "MAJOR_TYPE_DRAW", "draw": {"items": []}}
    feed.extend([dynamic(101, now[0]), blank])
    await tick(rig, seconds=300)
    assert service.list()[0]["last_session"] == "100"
    assert service.fetch_claim("10001") == []
    assert service.status()["last_error"] == "bilibili_dynamic_content_unavailable"
    assert service.list()[0]["source_health"]["last_failure_code"] == "bilibili_dynamic_content_unavailable"
    blank["modules"]["module_dynamic"]["major"] = {"type": "MAJOR_TYPE_OPUS",
        "opus": {"summary": {"text": "恢复的正文"}, "pics": []}}
    await tick(rig, seconds=600)  # Poll backoff is ten minutes after one failure.
    with service.db() as db:
        rows = db.execute("SELECT session,text FROM outbox ORDER BY session").fetchall()
    assert [r["session"] for r in rows] == ["101", "102"]
    assert "恢复的正文" in rows[1]["text"]


async def test_preview_with_only_live_recommendations_does_not_render_a_post(rig):
    service, _, feed, _, calls, _ = rig
    feed[0]["type"] = "DYNAMIC_TYPE_LIVE_RCMD"
    result = await service.preview({"type": "bilibili_dynamic", "input_type": "uid",
        "value": "456", "include_image": True})
    assert result["image_file"] == "" and result["image_error"] == ""
    assert "暂无可读的普通动态" in result["text"]
    assert len(calls) == 1 and service.fetch_claim("10001") == []


async def test_steam_initial_playing_debounce_switch_exit_and_restart(rig):
    service, now, _, players, *_ = rig
    players[0].update(gameid="570", gameextrainfo="Dota 2")
    await service.add(body("steam"))
    await tick(rig, 91); assert service.fetch_claim("10001") == []
    players[0].update(gameid="730", gameextrainfo="Counter-Strike 2")
    await tick(rig, 91); assert service.fetch_claim("10001") == []
    await tick(rig, 91)
    item = service.fetch_claim("10001")[0]; assert "Counter-Strike 2" in item["text"]
    service.ack(item["id"], item["claim_token"], "sent")
    restarted = BilibiliSubscriptions(service.root, client=service.client, clock=lambda: now[0])
    now[0] += 91; await restarted.poll_once(); assert restarted.fetch_claim("10001") == []
    players[0].pop("gameid"); players[0].pop("gameextrainfo")
    await tick(rig, 91); await tick(rig, 91)
    exit_item = service.fetch_claim("10001")[0]; assert "结束" in exit_item["text"]


async def test_steam_batch_transient_change_private_and_same_target_limit(rig):
    service, now, _, players, calls, _ = rig
    await service.add(body("steam"))
    sid2 = "76561198000000002"
    players.append(players[0] | {"steamid": sid2})
    await service.add(body("steam", value=sid2))
    before = len(calls); await tick(rig, 91)
    assert len(calls) == before + 1
    assert set(calls[-1].url.params["steamids"].split(",")) == {SID, sid2}
    players[0].update(personastate=0)
    await tick(rig, 91)
    players[0].update(personastate=1)
    await tick(rig, 91); assert service.fetch_claim("10001") == []
    players[0].update(communityvisibilitystate=1)
    await tick(rig, 91)
    assert service.status()["last_error"] == "steam_profile_private_or_unavailable"
    assert service.fetch_claim("10001") == []


async def test_shared_limit_across_dynamic_and_steam(rig):
    service, now, feed, players, *_ = rig
    await service.add(body()); await service.add(body("steam"))
    players[0].update(gameid="570", gameextrainfo="Dota 2")
    await tick(rig, 91); await tick(rig, 91)
    feed.append(dynamic(101, now[0])); await tick(rig, 301)
    assert service.status()["pending"] == 2
    assert len(service.fetch_claim("10001", 10)) == 1


async def test_preview_does_not_add_or_claim_and_has_cached_card(rig):
    service, _, _, _, calls, _ = rig
    data = {"type": "bilibili_dynamic", "input_type": "uid", "value": "https://space.bilibili.com/456", "include_image": True}
    preview = await service.preview(data)
    assert preview["preview_only"] and Path(preview["image_file"]).is_file()
    assert not service.list() and service.status()["pending"] == 0
    images = sum(r.url.host == "i0.hdslb.com" for r in calls)
    again = await service.preview(data)
    assert again["image_file"] == preview["image_file"]
    assert sum(r.url.host == "i0.hdslb.com" for r in calls) == images


async def test_preview_and_multiple_targets_use_each_events_own_content(rig, monkeypatch):
    service, now, feed, *_ = rig
    for target in ("20001", "20002"):
        await service.add(body(target_id=target, include_image=True))
    # The plain recipient should not trigger card generation.
    await service.add(body(target_id="30001", include_image=False) | {"kind":"dm"})
    calls = []
    async def render(info, text):
        calls.append((info, text))
        return "", ""
    monkeypatch.setattr(service.cards, "render", render)
    feed[:] = [dynamic(101, now[0], text="第一条的独立正文"),
               dynamic(102, now[0], text="第二条的独立正文")]
    await tick(rig)
    assert len(calls) == 2
    assert [info["session"] for info, _ in calls] == ["101", "102"]
    for info, text in calls:
        assert info["card"]["blocks"][0]["text"] in text
        assert info["published_at"] == 1000
        assert info["url"].endswith(info["session"])
    assert service.status()["pending"] == 6
    latest = calls[-1]
    calls.clear()
    await service.preview({"type":"bilibili_dynamic", "input_type":"uid", "value":"456", "include_image":True})
    assert calls == [latest]
    assert service.status()["pending"] == 6


async def test_credentials_missing_fail_closed_before_http(rig):
    service, _, _, _, calls, _ = rig
    update_credentials(service.root, {"clear": ["steam_api_key", "bilibili_sessdata"]})
    for kind in ("steam", "bilibili_dynamic"):
        with pytest.raises(BilibiliError, match="credentials_required"): await service.add(body(kind))
    assert not calls and not service.list()


async def test_dynamic_wrong_author_cannot_establish_a_baseline(rig):
    service, _, feed, *_ = rig
    feed[:] = [dynamic(100, uid=999)]
    with pytest.raises(BilibiliError, match="bilibili_dynamic_response_invalid"):
        await service.add(body())
    assert service.list() == []


async def test_pause_is_immediate_while_notification_card_is_loading(rig, monkeypatch):
    service, now, feed, *_ = rig
    sub = await service.add(body(include_image=True))
    started, release = asyncio.Event(), asyncio.Event()
    async def slow_card(info, text):
        started.set(); await release.wait()
        return "", "subscription_image_unavailable"
    monkeypatch.setattr(service.cards, "render", slow_card)
    feed.append(dynamic(101, now[0])); now[0] += 301
    polling = asyncio.create_task(service.poll_once())
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(service.update(sub["id"], {"enabled": False}), 1)
    release.set(); await polling
    assert service.fetch_claim("10001") == []
    assert not service.list()[0]["enabled"]


async def test_image_oversize_and_bad_mime_do_not_escape_as_a_card_failure(tmp_path):
    for response in (httpx.Response(200,content=b"x"*(5*1024*1024+1),headers={"content-type":"image/png"}),
                     httpx.Response(200,content=b"private response",headers={"content-type":"text/html"})):
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:response))
        cards=Cards(tmp_path,client)
        path,error=await cards.render({"type":"steam","name":"合成测试","cover":"https://avatars.steamstatic.com/image.png"},"消息")
        assert Path(path).is_file() and error=="subscription_image_unavailable"
        assert not list(cards.cache.glob("*.png"))


def test_credential_blank_keeps_delete_explicit_and_no_echo(tmp_path):
    update_credentials(tmp_path, {"steam_api_key": "synthetic-secret"})
    data = update_credentials(tmp_path, {"steam_api_key": ""})
    assert data["fields"]["steam_api_key"]["configured"]
    assert "synthetic-secret" not in json.dumps(data)
    update_credentials(tmp_path, {"clear": ["steam_api_key"]})
    assert not credential_status(tmp_path)["fields"]["steam_api_key"]["configured"]
    for invalid in ({"bilibili_sessdata": "bad\nheader"}, {"bilibili_sessdata": "x;foo=y"}, {"clear": [{}]}):
        with pytest.raises(ValueError): update_credentials(tmp_path, invalid)


def test_credentials_multifield_failure_rolls_back(tmp_path, monkeypatch):
    update_credentials(tmp_path, {"bilibili_sessdata": "old-session", "steam_api_key": "old-key"})
    original = CredentialStore.set
    def fail(self, name, value, base_url=""):
        if name == "steam_api_key" and value == "new-key": raise CredentialStoreError("synthetic_write_failure")
        return original(self, name, value, base_url)
    monkeypatch.setattr(CredentialStore, "set", fail)
    with pytest.raises(CredentialStoreError, match="subscription_credentials_write_failed"):
        update_credentials(tmp_path, {"bilibili_sessdata": "new-session", "steam_api_key": "new-key"})
    assert CredentialStore(tmp_path).get("bilibili_sessdata") == "old-session"
    assert CredentialStore(tmp_path).get("steam_api_key") == "old-key"


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "https://i0.hdslb.com@evil.test/x", "https://i0.hdslb.com:443/x", "https://steamcommunity.com/secret", "file:///etc/passwd", "https://i0.hdslb.com.evil.test/x"])
def test_card_url_rejects_untrusted_origins(url):
    assert safe_image_url(url) == ""


async def test_card_unsafe_url_no_request_and_bad_content_text_card_fallback(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    cards = Cards(tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    info = {"type": "steam", "name": "测试好友", "cover": "http://127.0.0.1/private", "avatar": ""}
    path, error = await cards.render(info, "测试通知")
    assert Path(path).exists() and not calls
    info["cover"] = "https://avatars.steamstatic.com/pic.png"
    path, error = await cards.render(info, "另一条通知")
    assert Path(path).exists() and error == "subscription_image_unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("kind,value", [("steam", "https://steamcommunity.com/id/name"), ("steam", "123"), ("bilibili_dynamic", "https://space.bilibili.com:443/456"), ("bilibili_dynamic", "https://b23.tv/x")])
def test_source_only_canonical_ids(kind, value):
    with pytest.raises(ValueError): source_id(kind, value)


def test_steam_newer_public_account_id_prefix_is_accepted():
    assert source_id("steam", "76561202255233023") == "76561202255233023"
    assert source_id("steam", "https://steamcommunity.com/profiles/76561202255233023") == "76561202255233023"


def test_legacy_schema_additive_migration_preserves_baseline_and_claim(tmp_path):
    path = tmp_path / "data/bilibili.sqlite3"; path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE rooms(room_id TEXT PRIMARY KEY,uid TEXT,name TEXT,title TEXT,live_status INTEGER,session TEXT,last_check REAL,next_poll REAL,failures INTEGER DEFAULT 0,last_error TEXT DEFAULT '');
        CREATE TABLE subscriptions(id TEXT PRIMARY KEY,room_id TEXT,self_id TEXT,kind TEXT,target_id TEXT,enabled INTEGER,last_status INTEGER,last_session TEXT,revision INTEGER DEFAULT 1,created_at REAL,updated_at REAL,UNIQUE(room_id,self_id,kind,target_id));
        CREATE TABLE outbox(id TEXT PRIMARY KEY,subscription_id TEXT,room_id TEXT,session TEXT,self_id TEXT,kind TEXT,target_id TEXT,revision INTEGER,text TEXT,status TEXT,claim_token TEXT,created_at REAL,claimed_at REAL,completed_at REAL,UNIQUE(subscription_id,session));
        INSERT INTO rooms VALUES ('123','456','原主播','原直播',1,'original-session',1000,1090,0,'');
        INSERT INTO subscriptions VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','123','10001','group','20001',1,1,'original-session',1,1000,1000);
        INSERT INTO outbox VALUES ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','123','old-session','10001','group','20001',1,'旧通知','uncertain','cccccccccccccccccccccccccccccccc',900,950,999);
        """)
    write_settings(tmp_path, Settings(transport="onebot",onebot_self_id="10001",onebot_enabled=True,onebot_allowed_groups=["20001"]))
    service = BilibiliSubscriptions(tmp_path, clock=lambda: 1000)
    row = service.list()[0]
    assert row["type"] == "bilibili_live" and not row["include_image"] and row["last_session"] == "original-session"
    assert service.fetch_claim("10001") == []
    assert service.status()["deliveries"][0]["status"] == "uncertain"
