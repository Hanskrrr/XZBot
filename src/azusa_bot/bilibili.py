"""Subscription sources sharing one scoped, at-most-once QQ outbox.

The historical class/database name is retained for backward compatibility.
Only the bridge sends QQ; this service collects, renders, claims and authorizes.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
from contextlib import contextmanager, closing
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid

import httpx

from .settings import read_settings, settings_lock
from .bot_control import activity_allowed, read_control
from .subscription_sources import Sources, SubscriptionError, TYPES, INTERVALS, source_id
from .subscription_cards import Cards
from .vendor.steam_monitor.transitions import classify_game_transition

BASE = "https://api.live.bilibili.com"
ROOM_ENDPOINT = "/room/v1/Room/get_info"
UID_ENDPOINT = "/room/v1/Room/get_status_info_by_uids"
POLL_SECONDS = 90
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SUBSCRIPTIONS = 100
MAX_BATCH_TARGETS = 50
OUTBOX_TTL_SECONDS = 900
DELIVERY_INTERVAL_SECONDS = 30


BilibiliError = SubscriptionError


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int,str)) or not re.fullmatch(r"[1-9][0-9]{0,19}",str(value)):
        raise ValueError("bilibili_numeric_id_required")
    return str(value)


def _identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r"[a-f0-9]{32}",value):
        raise ValueError("bilibili_subscription_id_invalid")
    return value


def _label(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _source(body):
    if not isinstance(body,dict) or body.get("input_type") not in {"room","uid"}:
        raise ValueError("bilibili_source_invalid")
    value = body.get("value")
    if not isinstance(value,str) or not 1<=len(value)<=300:
        raise ValueError("bilibili_source_invalid")
    value=value.strip()
    if value.isascii() and value.isdigit():
        return body["input_type"], _number(value)
    try:
        parsed=urlsplit(value)
        host="live.bilibili.com" if body["input_type"]=="room" else "space.bilibili.com"
        if (parsed.scheme not in {"http","https"} or parsed.hostname!=host or parsed.username or parsed.password
            or parsed.port is not None or not re.fullmatch(r"/[1-9][0-9]{0,19}/?",parsed.path)):
            raise ValueError()
        return body["input_type"],_number(parsed.path.strip("/"))
    except ValueError:
        raise ValueError("bilibili_source_requires_room_or_uid_url") from None


def _session(room_id, data):
    value=data.get("live_time",data.get("live_start_time"))
    if isinstance(value,int) and not isinstance(value,bool) and value>0:
        return room_id+":"+str(value)
    if isinstance(value,str):
        if re.fullmatch(r"[1-9][0-9]{8,12}",value):
            return room_id+":"+value
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",value):
                raise ValueError()
            datetime.strptime(value,"%Y-%m-%d %H:%M:%S")
            return room_id+":"+value
        except ValueError:
            pass
    raise BilibiliError("bilibili_live_session_unavailable")


class BilibiliSubscriptions:
    def __init__(self, root: Path, client=None, clock=time.time):
        self.root, self.client, self.clock=Path(root),client,clock
        self.path=self.root/"data/bilibili.sqlite3"
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not self.path.exists():
            descriptor=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(descriptor)
        self.path.chmod(0o600)
        self._lock=threading.RLock()
        self._operations=asyncio.Lock()
        self.activity_sources = lambda: set()
        self.activity_usage = lambda: {}
        self.activity_sync = lambda keys, backfill=False: []
        self.sources=Sources(self.root,client,clock=clock)
        self.cards=Cards(self.root,client)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS rooms(room_id TEXT PRIMARY KEY,uid TEXT,name TEXT,title TEXT,
              live_status INTEGER,session TEXT,last_check REAL,next_poll REAL,failures INTEGER DEFAULT 0,last_error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,room_id TEXT,self_id TEXT,
              kind TEXT,target_id TEXT,enabled INTEGER,last_status INTEGER,last_session TEXT,
              revision INTEGER DEFAULT 1,created_at REAL,updated_at REAL,
              UNIQUE(room_id,self_id,kind,target_id));
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,subscription_id TEXT,room_id TEXT,
              session TEXT,self_id TEXT,kind TEXT,target_id TEXT,revision INTEGER,text TEXT,status TEXT,
              claim_token TEXT,created_at REAL,claimed_at REAL,completed_at REAL,
              UNIQUE(subscription_id,session));
            CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status,created_at);
            """)
            # Additive migration: legacy rows retain live semantics and text-only
            # delivery. The old uniqueness constraints and delivery IDs survive.
            additions={"rooms":{"type":"TEXT NOT NULL DEFAULT 'bilibili_live'", "payload":"TEXT NOT NULL DEFAULT '{}'", "name_override":"TEXT NOT NULL DEFAULT ''", "source_revision":"INTEGER NOT NULL DEFAULT 1"},
                       "subscriptions":{"include_image":"INTEGER NOT NULL DEFAULT 0", "needs_rebaseline":"INTEGER NOT NULL DEFAULT 0"},
                       "outbox":{"image_file":"TEXT NOT NULL DEFAULT ''", "image_error":"TEXT NOT NULL DEFAULT ''", "type":"TEXT NOT NULL DEFAULT 'bilibili_live'", "run_id":"INTEGER", "source_key":"TEXT NOT NULL DEFAULT ''", "source_name":"TEXT NOT NULL DEFAULT ''"}}
            for table,columns in additions.items():
                existing={row[1] for row in db.execute("PRAGMA table_info("+table+")")}
                for column,definition in columns.items():
                    if column not in existing:db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            db.execute("CREATE TABLE IF NOT EXISTS subscription_media(path TEXT PRIMARY KEY,created_at REAL NOT NULL)")
            from .subscription_journal import initialize
            initialize(db)
            db.execute('CREATE INDEX IF NOT EXISTS outbox_run ON outbox(run_id)')
            db.execute('CREATE TABLE IF NOT EXISTS subscription_source_edits (id TEXT PRIMARY KEY, edits TEXT NOT NULL)')

    @contextmanager
    def db(self):
        with self._lock, closing(sqlite3.connect(self.path,timeout=10)) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db

    def close(self):
        """No persistent connection; injected HTTP client remains caller-owned."""

    def cancel_pending(self):
        """A full stop revokes claimed sends and requires a quiet fresh baseline."""
        with self.db() as db:
            db.execute("UPDATE outbox SET status='cancelled',completed_at=? WHERE status IN ('pending','claimed')",
                       (self.clock(),))
            db.execute("UPDATE subscriptions SET needs_rebaseline=1,revision=revision+1 WHERE enabled=1")

    def backup(self,path):
        destination=Path(path)
        if destination.exists() or destination.resolve()==self.path.resolve():
            raise ValueError("bilibili_backup_destination_exists")
        destination.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        destination.chmod(0o600)

    async def _request(self, endpoint, params):
        if endpoint not in {ROOM_ENDPOINT,UID_ENDPOINT}:
            raise ValueError("bilibili_endpoint_invalid")
        async def request(client):
            try:
                async with client.stream("GET",BASE+endpoint,params=params,
                    headers={"User-Agent":"AzusaPrivateBot/0.1 (+local live subscriptions)","Referer":"https://live.bilibili.com/"}) as response:
                    if response.status_code!=200:
                        raise BilibiliError("bilibili_http_"+str(response.status_code))
                    raw=bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw)>MAX_RESPONSE_BYTES:
                            raise BilibiliError("bilibili_response_too_large")
                import json
                data=json.loads(raw)
                if not isinstance(data,dict) or type(data.get("code")) is not int:
                    raise ValueError()
                if data["code"]!=0:
                    raise BilibiliError("bilibili_api_rejected")
                if not isinstance(data.get("data"),dict):
                    raise ValueError()
                return data["data"]
            except BilibiliError:
                raise
            except (httpx.HTTPError,ValueError,UnicodeError,TypeError):
                raise BilibiliError("bilibili_network_or_format") from None
        if self.client is not None:
            return await request(self.client)
        async with httpx.AsyncClient(timeout=httpx.Timeout(12,connect=5),trust_env=False,follow_redirects=False) as client:
            return await request(client)

    async def _room(self, room_id, name=""):
        data=await self._request(ROOM_ENDPOINT,{"room_id":_number(room_id)})
        try:
            room_id=_number(data.get("room_id"));uid=_number(data.get("uid"))
            state=data.get("live_status")
            if type(state) is not int or state not in {0,1,2}:
                raise ValueError()
            return dict(type="bilibili_live",source_id=room_id,room_id=room_id,uid=uid,name=_label(name or data.get("uname"),80),
                        title=_label(data.get("title"),160),live_status=state,
                        session=_session(room_id,data) if state==1 else "",url="https://live.bilibili.com/"+room_id,
                        cover=_label(data.get("user_cover") or data.get("cover"),2048),avatar="")
        except ValueError:
            raise BilibiliError("bilibili_room_response_invalid") from None

    async def resolve(self, body):
        if not isinstance(body,dict) or body.get("type","bilibili_live") not in TYPES:
            raise ValueError("subscription_type_invalid")
        subscription_type=body.get("type","bilibili_live")
        if subscription_type=="bilibili_video":
            return await self.sources.video(source_id(subscription_type,body.get("value")))
        if subscription_type=="bilibili_dynamic":
            if body.get("input_type","uid")!="uid":raise ValueError("subscription_source_invalid")
            return await self.sources.dynamic(source_id(subscription_type,body.get("value")))
        if subscription_type=="steam":
            if body.get("input_type","steamid")!="steamid":raise ValueError("subscription_source_invalid")
            return await self.sources.steam(source_id(subscription_type,body.get("value")))
        if subscription_type=="weibo":
            if body.get("input_type","uid")!="uid":raise ValueError("subscription_source_invalid")
            return await self.sources.weibo(source_id(subscription_type,body.get("value")), now=self.clock())
        kind,value=_source(body)
        if kind=="uid":
            data=await self._request(UID_ENDPOINT,{"uids[]":value})
            info=data.get(value)
            if not isinstance(info,dict):
                raise BilibiliError("bilibili_uid_has_no_room")
            try:
                room=_number(info.get("room_id"))
            except ValueError:
                raise BilibiliError("bilibili_uid_has_no_room") from None
            result=await self._room(room,_label(info.get("uname"),80))
            if result["uid"]!=value:
                raise BilibiliError("bilibili_identity_mismatch")
            return result
        return await self._room(value)

    async def _read_existing(self,row):
        if row["type"]=="bilibili_video":return await self.sources.video(row["uid"])
        if row["type"]=="bilibili_dynamic":return await self.sources.dynamic(row["uid"])
        if row["type"]=="steam":return await self.sources.steam(row["uid"])
        if row["type"]=="weibo":return await self.sources.weibo(row["uid"], now=self.clock())
        return await self._room(row["room_id"],row["name"])

    async def preview(self,body):
        if not isinstance(body,dict) or set(body)-{"type","input_type","value","include_image"} or type(body.get("include_image",True)) is not bool:
            raise ValueError("subscription_preview_fields_invalid")
        info=await self.resolve(body)
        event=(info["events"][-1] if info["type"] in {"bilibili_dynamic", "bilibili_video", "weibo"} and info["events"] else {})
        text=event.get("text") or f"{info['name'] or '订阅对象'}：{info['title']}\n{info['url']}"
        # Preview the same complete event (including timestamp and forwarded
        # content) that the outbox would render, without creating a delivery.
        renderable = info["type"] not in {"bilibili_dynamic", "bilibili_video", "weibo"} or bool(event)
        image_file,image_error=await self.cards.render(info|event,text) if body.get("include_image",True) and renderable else ("","")
        if image_file:self._track_media(image_file)
        return {key:info[key] for key in ("type","source_id","name","title","url")}|dict(text=text,image_file=image_file,
            image_error=image_error,include_image=body.get("include_image",True),preview_only=True)

    def _track_media(self,path):
        with self.db() as db:db.execute("INSERT OR REPLACE INTO subscription_media VALUES (?,?)",(path,self.clock()))

    def _prune_media(self):
        with self.db() as db:
            rows=db.execute("SELECT path FROM subscription_media WHERE created_at<? AND path NOT IN (SELECT image_file FROM outbox WHERE status IN ('pending','claimed'))",(self.clock()-7*86400,)).fetchall()
            for row in rows:
                path=Path(row[0])
                if path.parent==self.cards.media and re.fullmatch(r"[a-f0-9]{32}\.png",path.name) and not path.is_symlink():path.unlink(missing_ok=True)
                db.execute("DELETE FROM subscription_media WHERE path=?",(row[0],))

    def _target(self,kind,target_id):
        settings=read_settings(self.root)
        if settings.transport!="onebot" or not settings.onebot_self_id:
            raise ValueError("bilibili_delivery_transport_unsupported")
        target_id=_number(target_id)
        if kind not in {"group","dm"}:
            raise ValueError("bilibili_target_kind_invalid")
        allowed=settings.onebot_allowed_groups if kind=="group" else settings.onebot_allowed_dm_users
        if target_id not in allowed or (kind=="dm" and target_id==settings.onebot_self_id):
            raise ValueError("bilibili_target_not_allowed")
        return settings.onebot_self_id,kind,target_id

    def _allowed(self, row, bridge_self_id=None):
        if not activity_allowed(self.root):
            return False
        settings=read_settings(self.root)
        if (settings.transport!="onebot" or not settings.onebot_enabled or settings.onebot_self_id!=row["self_id"]
            or bridge_self_id is not None and bridge_self_id!=row["self_id"]):
            return False
        values=settings.onebot_allowed_groups if row["kind"]=="group" else settings.onebot_allowed_dm_users
        return row["target_id"] in values and not (row["kind"]=="dm" and row["target_id"]==row["self_id"])

    def _save_room(self,db,info):
        now=self.clock()
        payload={key:info[key] for key in ("cover","avatar","gameid") if key in info}
        if info['type'] in {'bilibili_dynamic','bilibili_video','weibo'}:
            payload['events']=info.get('events',[])[-20:]
        db.execute("""INSERT INTO rooms(room_id,uid,name,title,live_status,session,last_check,next_poll,failures,last_error,type,payload)
          VALUES (?,?,?,?,?,?,?,?,0,'',?,?) ON CONFLICT(room_id) DO UPDATE SET
          uid=excluded.uid,name=CASE WHEN rooms.name_override<>'' THEN rooms.name_override WHEN excluded.name='' THEN rooms.name ELSE excluded.name END,
          title=excluded.title,live_status=excluded.live_status,session=excluded.session,
          last_check=excluded.last_check,next_poll=excluded.next_poll,failures=0,last_error='',type=excluded.type,payload=excluded.payload""",
          (info["room_id"],info["uid"],info["name"],info["title"],info["live_status"],info["session"],now,
           now+INTERVALS[info["type"]],info["type"],json.dumps(payload)))

    async def add(self,body,*,authorize=None):
        if not isinstance(body,dict) or set(body)-{"type","input_type","value","kind","target_id","enabled","include_image"} or type(body.get("enabled",False)) is not bool or type(body.get("include_image",True)) is not bool:
            raise ValueError("bilibili_subscription_fields_invalid")
        if authorize:authorize()
        identity=self._target(body.get("kind"),body.get("target_id"))
        async with self._operations:
            if authorize:authorize()
            with self.db() as db:
                if db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]>=MAX_SUBSCRIPTIONS:
                    raise ValueError("bilibili_subscription_limit")
            info=await self.resolve(body)
            if identity!=self._target(body.get("kind"),body.get("target_id")):
                raise ValueError("bilibili_target_changed")
            number,now=uuid.uuid4().hex,self.clock()
            try:
                with settings_lock, self.db() as db:
                    if authorize:authorize()
                    if identity!=self._target(body.get("kind"),body.get("target_id")):
                        raise ValueError("bilibili_target_changed")
                    self._save_room(db,info)
                    db.execute("INSERT INTO subscriptions(id,room_id,self_id,kind,target_id,enabled,last_status,last_session,revision,created_at,updated_at,include_image) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                      (number,info["room_id"],*identity,int(body.get("enabled",False)),info["live_status"],info["session"],now,now,int(body.get("include_image",True))))
            except sqlite3.IntegrityError:
                raise ValueError("bilibili_subscription_exists") from None
            return next(row for row in self.list() if row["id"]==number)

    async def add_many(self,body,*,authorize=None):
        """One source snapshot, atomically added to explicitly selected targets.

        Existing source/target rows are untouched, including paused state and
        delivery baselines. The existing poller already fetches once per source
        and fans out through independently authorized subscription rows.
        """
        if (not isinstance(body,dict)
                or set(body)-{"type","input_type","value","targets","enabled","include_image"}
                or "type" in body and not isinstance(body["type"],str)
                or "input_type" in body and not isinstance(body["input_type"],str)
                or type(body.get("enabled",False)) is not bool
                or type(body.get("include_image",True)) is not bool):
            raise ValueError("bilibili_subscription_fields_invalid")
        targets=body.get("targets")
        if (not isinstance(targets,list) or not 1<=len(targets)<=MAX_BATCH_TARGETS
                or any(not isinstance(target,dict) or set(target)!={"kind","target_id"}
                       or not isinstance(target["kind"],str) for target in targets)):
            raise ValueError("bilibili_subscription_targets_invalid")
        # Take an immutable local copy before a source read yields control.
        targets=[dict(target) for target in targets]
        def identities():
            with settings_lock:
                return list(dict.fromkeys(self._target(target["kind"],target["target_id"]) for target in targets))
        if authorize:authorize()
        expected=identities()
        async with self._operations:
            if authorize:authorize()
            if identities()!=expected:raise ValueError("bilibili_target_changed")
            info=await self.resolve(body)
            if identities()!=expected:raise ValueError("bilibili_target_changed")
            created=[]
            with settings_lock, self.db() as db:
                if authorize:authorize()
                if identities()!=expected:raise ValueError("bilibili_target_changed")
                # A single SQLite write transaction also protects against a
                # second service object performing the same batch concurrently.
                db.execute("BEGIN IMMEDIATE")
                existing={(row["self_id"],row["kind"],row["target_id"]) for row in db.execute(
                    "SELECT self_id,kind,target_id FROM subscriptions WHERE room_id=?",(info["room_id"],))}
                additions=[identity for identity in expected if identity not in existing]
                count=db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
                if count+len(additions)>MAX_SUBSCRIPTIONS:
                    raise ValueError("bilibili_subscription_limit")
                if additions:
                    # Do not move an existing source's poll deadline or erase a
                    # pending Steam observation just because a target is added.
                    if not db.execute("SELECT 1 FROM rooms WHERE room_id=?",(info["room_id"],)).fetchone():
                        self._save_room(db,info)
                    now=self.clock()
                    for identity in additions:
                        number=uuid.uuid4().hex
                        db.execute("INSERT INTO subscriptions(id,room_id,self_id,kind,target_id,enabled,last_status,last_session,revision,created_at,updated_at,include_image) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                            (number,info["room_id"],*identity,int(body.get("enabled",False)),info["live_status"],info["session"],now,now,int(body.get("include_image",True))))
                        created.append(number)
            created_ids=set(created)
            return {"created_count":len(created),"existing_count":len(expected)-len(created),
                    "subscriptions":[row for row in self.list() if row["id"] in created_ids]}

    async def update(self,number,body,*,authorize=None):
        _identifier(number)
        if authorize:authorize()
        if not isinstance(body,dict) or not body or set(body)-{"enabled","kind","target_id","include_image"} or "enabled" in body and type(body["enabled"]) is not bool or "include_image" in body and type(body["include_image"]) is not bool:
            raise ValueError("bilibili_subscription_fields_invalid")
        if body == {"enabled": False}:
            # Revocation must not wait behind a slow public HTTP request.
            with settings_lock, self.db() as db:
                if authorize:authorize()
                if not db.execute("UPDATE subscriptions SET enabled=0,revision=revision+1,updated_at=? WHERE id=?",(self.clock(),number)).rowcount:
                    raise ValueError("bilibili_subscription_not_found")
                db.execute("UPDATE outbox SET status='cancelled',completed_at=? WHERE subscription_id=? AND status IN ('pending','claimed')",(self.clock(),number))
            return next(row for row in self.list() if row["id"]==number)
        async with self._operations:
            if authorize:authorize()
            with self.db() as db:
                old=db.execute("SELECT * FROM subscriptions WHERE id=?",(number,)).fetchone()
                if not old:raise ValueError("bilibili_subscription_not_found")
                old=dict(old)
            identity=self._target(body.get("kind",old["kind"]),body.get("target_id",old["target_id"]))
            if identity[0]!=old["self_id"]:
                raise ValueError("bilibili_account_changed_recreate_subscription")
            enabled=body.get("enabled",bool(old["enabled"]))
            changed=identity[1:]!=(old["kind"],old["target_id"])
            refresh=enabled and (not old["enabled"] or changed)
            with self.db() as db:room=dict(db.execute("SELECT * FROM rooms WHERE room_id=?",(old["room_id"],)).fetchone())
            info=await self._read_existing(room) if refresh else None
            if identity!=self._target(identity[1],identity[2]):
                raise ValueError("bilibili_target_changed")
            try:
                with settings_lock, self.db() as db:
                    if authorize:authorize()
                    if identity!=self._target(identity[1],identity[2]):
                        raise ValueError("bilibili_target_changed")
                    current=db.execute("SELECT revision FROM subscriptions WHERE id=?",(number,)).fetchone()
                    if not current or current[0]!=old["revision"]:raise ValueError("bilibili_subscription_changed")
                    if info:self._save_room(db,info)
                    db.execute("UPDATE subscriptions SET kind=?,target_id=?,enabled=?,last_status=?,last_session=?,revision=revision+1,updated_at=?,include_image=? WHERE id=?",
                        (identity[1],identity[2],int(enabled),info["live_status"] if info else old["last_status"],info["session"] if info else old["last_session"],self.clock(),int(body.get("include_image",bool(old["include_image"]))),number))
                    db.execute("UPDATE outbox SET status='cancelled',completed_at=? WHERE subscription_id=? AND status IN ('pending','claimed')",(self.clock(),number))
            except sqlite3.IntegrityError:
                raise ValueError("bilibili_subscription_exists") from None
            return next(row for row in self.list() if row["id"]==number)

    def delete(self,number,*,authorize=None):
        _identifier(number)
        with settings_lock, self.db() as db:
            if authorize:authorize()
            if not db.execute("DELETE FROM subscriptions WHERE id=?",(number,)).rowcount:
                raise ValueError("bilibili_subscription_not_found")
            db.execute("UPDATE outbox SET status='cancelled',completed_at=? WHERE subscription_id=? AND status IN ('pending','claimed')",(self.clock(),number))
            # Sources have their own lifecycle; removing a push does not erase a role source.
        return {"deleted":True}

    def list(self):
        with self.db() as db:
            rows=db.execute("""SELECT s.*,r.type,r.uid,r.name,r.title,r.live_status,r.last_check,r.next_poll,r.last_error
                FROM subscriptions s JOIN rooms r ON r.room_id=s.room_id ORDER BY s.created_at,s.id""").fetchall()
            health=self.sources.cooldown.health_many(("bilibili_dynamic" if row["type"]=="bilibili_video" else row["type"])+":"+str(row["uid"]) for row in rows)
            result=[]
            for item in rows:
                row=dict(item);row["enabled"]=bool(row["enabled"]);row["include_image"]=bool(row["include_image"])
                row["source_id"]=row["room_id"] if row["type"]=="bilibili_live" else row["uid"]
                url=("https://live.bilibili.com/"+row["room_id"] if row["type"]=="bilibili_live" else
                     "https://space.bilibili.com/"+row["uid"]+("/video" if row["type"]=="bilibili_video" else "/dynamic") if row["type"] in {"bilibili_dynamic","bilibili_video"} else
                     "https://m.weibo.cn/u/"+row["uid"] if row["type"]=="weibo" else "https://steamcommunity.com/profiles/"+row["uid"])
                row.update(transport="onebot",url=url,delivery_allowed=self._allowed(row))
                row["source_health"]=health[("bilibili_dynamic" if row["type"]=="bilibili_video" else row["type"])+":"+str(row["uid"])]
                result.append(row)
            return result

    def _expire(self,db):
        now=self.clock()
        db.execute("UPDATE outbox SET status='expired',completed_at=? WHERE status='pending' AND created_at<?",(now,now-OUTBOX_TTL_SECONDS))
        db.execute("UPDATE outbox SET status='uncertain',completed_at=? WHERE status='claimed' AND claimed_at<?",(now,now-120))

    def status(self):
        with self.db() as db:
            self._expire(db)
            rows=[dict(row) for row in db.execute("SELECT id,subscription_id,type,room_id,kind,target_id,status,image_error,created_at,claimed_at,completed_at,text,session,source_key,source_name,run_id FROM outbox ORDER BY created_at DESC LIMIT 30")]
            from .subscription_journal import clean
            for row in rows:row['text']=clean(row['text'],1200)
            room=db.execute("SELECT MAX(last_check),MIN(next_poll) FROM rooms WHERE room_id IN (SELECT room_id FROM subscriptions WHERE enabled=1)").fetchone()
            error=db.execute("SELECT last_error FROM rooms WHERE last_error<>'' ORDER BY last_check DESC LIMIT 1").fetchone()
            return {"poll_interval_seconds":POLL_SECONDS,"poll_intervals":INTERVALS,"delivery_interval_seconds":DELIVERY_INTERVAL_SECONDS,"subscriptions":db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
              "enabled":db.execute("SELECT COUNT(*) FROM subscriptions WHERE enabled=1").fetchone()[0],
              "pending":db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0],
              "last_poll_at":room[0],"next_poll_at":room[1],"last_error":error[0] if error else "","deliveries":rows}

    async def refresh(self, keys, *, notify=False):
        from .source_catalog import validate_key
        if (not isinstance(keys,list) or not 1<=len(keys)<=20 or type(notify) is not bool
                or any(not isinstance(key,str) for key in keys)):
            raise ValueError('subscription_refresh_invalid')
        keys=list(dict.fromkeys(keys))
        for key in keys:validate_key(key)
        if self._operations.locked():raise ValueError('subscription_refresh_busy')
        if notify and not activity_allowed(self.root):raise ValueError('subscription_refresh_bot_paused')
        return await self.poll_once(manual_keys=keys,notify=notify)

    async def sync_activity(self, keys):
        """Record cached imports after an explicit role-binding change; no HTTP."""
        from . import subscription_journal as journal
        from .source_catalog import source_key
        async with self._operations:
            with self.db() as db:rooms=[dict(row) for row in db.execute('SELECT * FROM rooms') if source_key(row) in keys]
            roles=await asyncio.to_thread(self.activity_sync,set(keys))
            for room in rooms:
                number=source_key(room).split(':',1)[1]
                run=journal.begin(self,room,'binding')
                info=room|json.loads(room['payload'])|{'source_id':number,'url':journal.source_url(room['type'],number)}
                journal.finish(self,run,info=info,cached=True,state='success' if room['last_check'] and not room['last_error'] else 'waiting',
                               roles=[row for row in roles if row['source_key']==source_key(room)])

    async def poll_once(self, *, manual_keys=None, notify=True):
        from . import subscription_journal as journal
        from .source_catalog import source_key
        manual=manual_keys is not None
        epoch = read_control(self.root)["epoch"]
        def stopped():return (not manual or notify) and not activity_allowed(self.root,epoch)
        if stopped():return {"checked":0}
        if self._operations.locked():return {"checked":0}
        async with self._operations:
            if stopped():return {"checked":0}
            self._prune_media()
            role_sources = self.activity_sources()
            with self.db() as db:
                if db.execute('SELECT 1 FROM subscription_source_edits LIMIT 1').fetchone():
                    if manual:raise ValueError('subscription_source_edit_incomplete')
                    return {'checked':0}
                self._expire(db)
                active_rooms = {row[0] for row in db.execute("SELECT DISTINCT room_id FROM subscriptions WHERE enabled=1")}
                all_rooms=[dict(row) for row in db.execute('SELECT * FROM rooms')]
                if manual and set(manual_keys)-{source_key(row) for row in all_rooms}:
                    raise ValueError('subscription_source_not_found')
                due = [row for row in all_rooms if (source_key(row) in manual_keys if manual else
                       row['next_poll']<=self.clock() and (row['room_id'] in active_rooms or source_key(row) in role_sources))]
            steam,steam_error={},None
            def fresh_cache(row):return manual and row['last_check']>0 and not row['last_error'] and 0<=self.clock()-row['last_check']<30
            steam_ids=[row["uid"] for row in due if row["type"]=="steam" and not fresh_cache(row)]
            if steam_ids:
                try:steam=await self.sources.steam_many(steam_ids)
                except BilibiliError as error:steam_error=error
            checked=0
            runs=[]
            dynamic_reads={}
            due.sort(key=lambda row:row["type"]=="bilibili_video")
            for room in due:
                if stopped():return {"checked":checked, **({"run_ids":runs} if manual else {})}
                run=journal.begin(self,room,'manual' if manual else 'scheduled');runs.append(run)
                cached=False
                try:
                    if fresh_cache(room):
                        cached=True
                        info=room|json.loads(room['payload'])|{'source_id':source_key(room).split(':',1)[1],
                             'url':journal.source_url(room['type'],source_key(room).split(':',1)[1])}
                    elif room["type"]=="steam":
                        if steam_error:raise steam_error
                        if room["uid"] not in steam:raise BilibiliError("steam_profile_private_or_unavailable")
                        info=steam[room["uid"]]
                    elif room["type"] in {"bilibili_dynamic","bilibili_video"}:
                        uid=room["uid"]
                        if uid not in dynamic_reads:
                            try:dynamic_reads[uid]=await self._read_existing(room)
                            except BilibiliError as exc:dynamic_reads[uid]=exc
                        snapshot=dynamic_reads[uid]
                        if isinstance(snapshot,BilibiliError):raise snapshot
                        info=self.sources.videos_from_dynamic(snapshot) if room["type"]=="bilibili_video" and snapshot["type"]!="bilibili_video" else snapshot
                    else:info=await self._read_existing(room)
                    if stopped():
                        journal.finish(self,run,state='cancelled');return {"checked":checked, **({"run_ids":runs} if manual else {})}
                    if info["room_id"] != room["room_id"] or (room["uid"] and info["uid"] != room["uid"]):
                        raise BilibiliError("bilibili_identity_mismatch")
                except BilibiliError as error:
                    if stopped():
                        journal.finish(self,run,state='cancelled');return {"checked":checked, **({"run_ids":runs} if manual else {})}
                    with self.db() as db:
                        now=self.clock()
                        # A preview may already hold the source lease or have
                        # scheduled a pause. Neither is a new failed HTTP read;
                        # honor its deadline without compounding poll backoff.
                        admission_wait = str(error) in {
                            "bilibili_dynamic_cooldown", "bilibili_dynamic_request_in_progress",
                            "weibo_cooldown", "weibo_request_in_progress",
                        } and error.retry_at > now
                        failures=room["failures"] if admission_wait else min(room["failures"]+1,8)
                        next_poll=error.retry_at if admission_wait else max(
                            now+min(3600,INTERVALS[room["type"]]*2**failures),error.retry_at)
                        db.execute("UPDATE rooms SET last_check=?,next_poll=?,failures=?,last_error=? WHERE room_id=?",
                            (now,next_poll,failures,str(error),room["room_id"]))
                    with self.db() as db:
                        targets=[{'subscription_id':r['id'],'kind':r['kind'],'target_id':r['target_id'],'reason':'source_failed'} for r in db.execute('SELECT id,kind,target_id FROM subscriptions WHERE room_id=?',(room['room_id'],))]
                    try:roles=await asyncio.to_thread(self.activity_sync,{source_key(room)})
                    except Exception:roles=[]
                    journal.finish(self,run,state='deferred' if admission_wait else 'failed',error=str(error),retry_at=next_poll,targets=targets,roles=roles)
                    continue
                if room.get('name_override'):info=info|{'name':room['name_override']}
                # Require two successful observations of a Steam change, avoiding
                # most single-poll transient disconnect/start notifications.
                with self.db() as db:
                    if not db.execute("SELECT 1 FROM rooms WHERE room_id=?",(room["room_id"],)).fetchone():continue
                    if cached and room['type']=='steam' and json.loads(room['payload']).get('candidate_session'):
                        checked+=1
                        journal.finish(self,run,info=info,state='confirming',cached=True,db=db)
                        continue
                    if room["type"]=="steam" and room["session"]!=info["session"]:
                        payload=json.loads(room["payload"])
                        if payload.get("candidate_session")!=info["session"]:
                            payload["candidate_session"]=info["session"]
                            db.execute("UPDATE rooms SET payload=?,last_check=?,next_poll=?,last_error='',failures=0 WHERE room_id=?",
                                (json.dumps(payload),self.clock(),self.clock()+INTERVALS["steam"],room["room_id"]))
                            checked+=1
                            journal.finish(self,run,info=info,state='confirming',db=db)
                            continue
                    subs=[dict(row) for row in db.execute("SELECT * FROM subscriptions WHERE room_id=?",(room["room_id"],))]
                deliveries=[]
                targets=[]
                rendered_events={}
                for sub in subs:
                    reason=('paused' if not sub['enabled'] else 'manual_sync_only' if not notify else
                            'baseline' if sub['needs_rebaseline'] else 'not_allowed' if not self._allowed(sub) else 'no_new_event')
                    target={'subscription_id':sub['id'],'kind':sub['kind'],'target_id':sub['target_id'],'reason':reason}
                    targets.append(target)
                    if reason!='no_new_event':continue
                    events=self._events(info,sub)
                    if events:target['reason']='queued'
                    for event in events:
                        if stopped():
                            journal.finish(self,run,state='cancelled');return {"checked":checked, **({"run_ids":runs} if manual else {})}
                        image_file,image_error="",""
                        if sub["include_image"]:
                            # One event can target many groups. Reuse its card
                            # within this poll; every recipient still passes
                            # the permission/revision checks below.
                            if event["session"] not in rendered_events:
                                rendered_events[event["session"]]=await self.cards.render(info|event,event["text"])
                            image_file,image_error=rendered_events[event["session"]]
                        deliveries.append((sub,event,image_file,image_error))
                with self.db() as db:
                    if stopped():
                        journal.finish(self,run,state='cancelled',db=db);return {"checked":checked, **({"run_ids":runs} if manual else {})}
                    # Pause/delete can revoke during image HTTP or rendering.
                    if not db.execute("SELECT 1 FROM rooms WHERE room_id=?",(room["room_id"],)).fetchone():continue
                    if not cached:self._save_room(db,info)
                    checked+=1
                    if notify and room["type"]=="bilibili_live":
                        db.execute("UPDATE outbox SET status='expired',completed_at=? WHERE room_id=? AND status='pending' AND session<>?",
                            (self.clock(),room["room_id"],info["session"]))
                    elif notify and room["type"]=="steam" and room["session"]!=info["session"]:
                        db.execute("UPDATE outbox SET status='expired',completed_at=? WHERE room_id=? AND status='pending'",
                            (self.clock(),room["room_id"]))
                    for sub,event,image_file,image_error in deliveries:
                        current=db.execute("SELECT * FROM subscriptions WHERE id=?",(sub["id"],)).fetchone()
                        if not current or not current["enabled"] or current["revision"]!=sub["revision"] or not self._allowed(current):continue
                        db.execute("""INSERT OR IGNORE INTO outbox(id,subscription_id,room_id,session,self_id,kind,target_id,revision,text,status,created_at,image_file,image_error,type,run_id,source_key,source_name)
                          VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)""",
                          (uuid.uuid4().hex,sub["id"],info["room_id"],event["session"],sub["self_id"],sub["kind"],sub["target_id"],sub["revision"],event["text"],self.clock(),image_file,image_error,info["type"],run,source_key(room),journal.clean(info['name'],80)))
                        if image_file:db.execute("INSERT OR REPLACE INTO subscription_media VALUES (?,?)",(image_file,self.clock()))
                    for sub in subs:
                        if not notify:continue  # A local import must not consume QQ's send cursor.
                        # Always advance observation baselines, even if delivery is
                        # disabled globally, so later enabling cannot flood history.
                        baseline=info["session"]
                        if info["type"] in {"bilibili_dynamic", "bilibili_video", "weibo"}:baseline=str(max(int(sub["last_session"] or "0"),int(baseline)))
                        db.execute("UPDATE subscriptions SET last_status=?,last_session=?,needs_rebaseline=0 WHERE id=? AND enabled=1 AND revision=?",
                            (info["live_status"],baseline,sub["id"],sub["revision"]))
                try:
                    roles=await asyncio.to_thread(self.activity_sync,{source_key(room)},backfill=manual)
                except Exception:
                    journal.finish(self,run,info=info,cached=cached,targets=targets,state='partial',error='role_activity_sync_failed')
                else:
                    journal.finish(self,run,info=info,cached=cached,targets=targets,roles=roles,
                                   state='partial' if any(r.get('status')=='failed' for r in roles) else 'success',
                                   error='role_activity_archive_failed' if any(r.get('status')=='failed' for r in roles) else '')
            return {"checked":checked, **({'run_ids':runs} if manual else {})}

    def _events(self,info,sub):
        if info["type"]=="bilibili_live":
            if info["live_status"]!=1 or sub["last_status"]==1 and sub["last_session"]==info["session"]:return []
            return [{"session":info["session"],"text":f"{info['name'] or '你订阅的直播间'}开播啦：{info['title'] or '来看看这场直播吧'}\n{info['url']}"}]
        if info["type"] in {"bilibili_dynamic", "bilibili_video", "weibo"}:
            # Numeric dynamic IDs are creation-ordered; old pinned items never
            # lower the persistent high-water mark. At most five recent items.
            return [event for event in info["events"] if int(event["session"])>int(sub["last_session"] or "0")
                    and 0<=self.clock()-event["published_at"]<=OUTBOX_TTL_SECONDS][-5:]
        if sub["last_session"]==info["session"]:return []
        previous,current=json.loads(sub["last_session"]),json.loads(info["session"])
        transition=classify_game_transition(previous,current)
        name=info["name"]
        if transition.has_start:text=f"{name}开始玩 {info['title']} 了"
        elif transition.has_exit:text=f"{name}结束了刚才的游戏"
        elif current["online"]!=previous["online"]:text=f"{name}{'上线' if current['online'] else '下线'}了"
        else:return []
        session=info["room_id"]+":"+str(self.clock())+":"+hashlib.sha256(info["session"].encode()).hexdigest()[:12]
        return [{"session":session,"text":text+"\n"+info["url"]}]

    def fetch_claim(self,bridge_self_id,limit=5):
        bridge_self_id=_number(bridge_self_id)
        if type(limit) is not int or not 1<=limit<=10:raise ValueError("bilibili_claim_limit_invalid")
        if not activity_allowed(self.root):return []
        result=[]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE");self._expire(db)
            rows=db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created_at,id").fetchall()
            for row in rows:
                sub=db.execute("SELECT * FROM subscriptions WHERE id=?",(row["subscription_id"],)).fetchone()
                if not sub or not sub["enabled"] or sub["revision"]!=row["revision"] or not self._allowed(row):
                    db.execute("UPDATE outbox SET status='cancelled',completed_at=? WHERE id=?",(self.clock(),row["id"]));continue
                if row["self_id"]!=bridge_self_id:continue
                latest=db.execute("SELECT MAX(claimed_at) FROM outbox WHERE self_id=? AND kind=? AND target_id=?",
                    (row["self_id"],row["kind"],row["target_id"])).fetchone()[0]
                if latest is not None and self.clock()-latest<DELIVERY_INTERVAL_SECONDS:
                    continue
                token=uuid.uuid4().hex
                db.execute("UPDATE outbox SET status='claimed',claim_token=?,claimed_at=? WHERE id=? AND status='pending'",(token,self.clock(),row["id"]))
                result.append({key:row[key] for key in ("id","self_id","kind","target_id","text","room_id","session","image_file","type")}|{"claim_token":token,"transport":"onebot"})
                if len(result)>=limit:break
        return result

    def authorize_claim(self,number,claim_token,bridge_self_id):
        _identifier(number);_identifier(claim_token);bridge_self_id=_number(bridge_self_id)
        with self.db() as db:
            self._expire(db)
            row=db.execute("SELECT * FROM outbox WHERE id=? AND claim_token=? AND status='claimed'",(number,claim_token)).fetchone()
            if not row:return False
            sub=db.execute("SELECT * FROM subscriptions WHERE id=?",(row["subscription_id"],)).fetchone()
            allowed=bool(sub and sub["enabled"] and sub["revision"]==row["revision"] and self._allowed(row,bridge_self_id))
            if not allowed:db.execute("UPDATE outbox SET status='cancelled',completed_at=? WHERE id=?",(self.clock(),number))
            return allowed

    def ack(self,number,claim_token,status):
        _identifier(number);_identifier(claim_token)
        if status not in {"sent","uncertain","failed"}:raise ValueError("bilibili_ack_status_invalid")
        with self.db() as db:
            changed=db.execute("UPDATE outbox SET status=?,completed_at=? WHERE id=? AND claim_token=? AND status='claimed'",(status,self.clock(),number,claim_token)).rowcount
        return {"acknowledged":bool(changed)}
