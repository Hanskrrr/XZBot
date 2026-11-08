"""Bounded, ephemeral group context and durable content-free participation state.

The bridge supplies trusted scope/member/message IDs. Display names and text are
untrusted conversation data, never permissions. Only public familiarity metadata,
opt-outs, event hashes and reserved/sent counters are persisted; chat text is not.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Callable
import uuid


class GroupConversation:
    def __init__(self, path: str | Path, *, max_scopes: int = 64, max_messages: int = 100,
                 max_members: int = 128, ttl_seconds: float = 7200,
                 max_text_chars: int = 2000, clock: Callable[[], float] = time.time):
        if not 1 <= max_scopes <= 256 or not 1 <= max_messages <= 100 or not 1 <= max_members <= 512:
            raise ValueError("Invalid group context limits")
        if not 1 <= ttl_seconds <= 7200 or not 1 <= max_text_chars <= 10000:
            raise ValueError("Invalid group text/retention limits")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.path.chmod(0o600)
        self.max_scopes, self.max_messages, self.max_members = max_scopes, max_messages, max_members
        self.ttl_seconds, self.max_text_chars, self.clock = ttl_seconds, max_text_chars, clock
        self._guard = threading.RLock()
        self._rooms: OrderedDict[str, dict] = OrderedDict()
        self._revision = 0
        self._scope_limits = OrderedDict()
        self._locks = [asyncio.Lock() for _ in range(64)]
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS group_members (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT NOT NULL,
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL, message_count INTEGER NOT NULL,
                    PRIMARY KEY(scope,user_id));
                CREATE TABLE IF NOT EXISTS group_optouts (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY(scope,user_id));
                CREATE TABLE IF NOT EXISTS group_policy (key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                INSERT OR IGNORE INTO group_policy VALUES ('global_optout',0);
                CREATE TABLE IF NOT EXISTS group_seen (
                    event_hash TEXT PRIMARY KEY,scope TEXT NOT NULL,user_id TEXT NOT NULL,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS group_replies (
                    id TEXT PRIMARY KEY,scope TEXT NOT NULL,at REAL NOT NULL,status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS group_heat_replies (id TEXT PRIMARY KEY,scope TEXT NOT NULL,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS group_silence (scope TEXT PRIMARY KEY, since REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS group_activity (scope TEXT PRIMARY KEY,at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS group_reply_scope ON group_replies(scope,at);
                CREATE INDEX IF NOT EXISTS group_member_seen ON group_members(scope,last_seen);
            """)
            self._prune_persistent(db, self.clock())

    @contextmanager
    def db(self):
        """Short transaction for consistent local administration/backup; no text."""
        with self._guard:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA secure_delete=ON")
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    @staticmethod
    def _id(value: str) -> str:
        if not isinstance(value,str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid stable identity")
        return value

    def _next(self):
        self._revision += 1
        return self._revision

    def configure_scope(self, scope, max_messages, ttl_seconds):
        with self._guard:
            self._scope_limits[scope]=(max_messages,ttl_seconds)
            self._scope_limits.move_to_end(scope)
            while len(self._scope_limits)>self.max_scopes:
                old,_=self._scope_limits.popitem(last=False)
                self._rooms.pop(old,None)
            self._prune_memory(self.clock())
            if scope in self._rooms:
                self._rooms[scope]["messages"]=deque(list(self._rooms[scope]["messages"])[-max_messages:])

    def silence_seconds(self, scope, *, initialize=True):
        with self.db() as db:
            now=self.clock()
            if initialize:
                db.execute("INSERT OR IGNORE INTO group_silence VALUES (?,?)",(scope,now))
            row=db.execute("SELECT since FROM group_silence WHERE scope=?",(scope,)).fetchone()
            db.execute("DELETE FROM group_silence WHERE scope NOT IN (SELECT scope FROM group_silence ORDER BY since DESC LIMIT ?)",(self.max_scopes*4,))
            return max(0.,now-row[0]) if row else 0.

    def _prune_memory(self, now: float):
        for scope, room in list(self._rooms.items()):
            kept = deque(row for row in room["messages"] if now - row["at"] < self._scope_limits.get(scope,(self.max_messages,self.ttl_seconds))[1])
            if not kept:
                del self._rooms[scope]
            elif len(kept) != len(room["messages"]):
                room["messages"] = kept
                room["version"] = self._next()

    def _prune_persistent(self, db, now):
        db.execute("DELETE FROM group_seen WHERE at<=?", (now-self.ttl_seconds,))
        db.execute("DELETE FROM group_seen WHERE rowid NOT IN (SELECT rowid FROM group_seen ORDER BY at DESC,rowid DESC LIMIT ?)",
                   (self.max_scopes*self.max_messages*4,))
        db.execute("DELETE FROM group_replies WHERE at<=?", (now-86400,))
        db.execute("DELETE FROM group_heat_replies WHERE at<=?",(now-600,))
        db.execute("DELETE FROM group_activity WHERE at<=?",(now-86400,))

    def _opted_out(self, db, scope, user):
        return bool(db.execute("SELECT value FROM group_policy WHERE key='global_optout'").fetchone()[0]
                    or db.execute("SELECT 1 FROM group_optouts WHERE scope=? AND user_id=?", (scope,user)).fetchone())

    def opted_out(self, scope: str, user_id: str) -> bool:
        self._id(scope); self._id(user_id)
        with self.db() as db:
            return self._opted_out(db,scope,user_id)

    def set_optout(self, scope: str, user_id: str, enabled: bool = True):
        """Persist exclusion from both listening context and familiarity counts.

        Opt-out tombstones are never silently evicted. At their hard global
        capacity (scopes * members), further opt-outs fail closed by disabling
        all new collection; this avoids unbounded identity storage or forgetting
        an earlier refusal. That rare global stop needs local administrator review.
        """
        self._id(scope); self._id(user_id)
        if not isinstance(enabled,bool):
            raise ValueError("Opt-out must be boolean")
        with self._guard, self.db() as db:
            if enabled:
                exists = db.execute("SELECT 1 FROM group_optouts WHERE scope=? AND user_id=?", (scope,user_id)).fetchone()
                count = db.execute("SELECT COUNT(*) FROM group_optouts").fetchone()[0]
                if not exists and count >= self.max_scopes*self.max_members:
                    db.execute("UPDATE group_policy SET value=1 WHERE key='global_optout'")
                    db.execute("DELETE FROM group_members")
                    db.execute("DELETE FROM group_seen")
                    self._rooms.clear()
                else:
                    db.execute("INSERT OR IGNORE INTO group_optouts VALUES (?,?)", (scope,user_id))
                self._remove_user_locked(db,scope,user_id)
            else:
                db.execute("DELETE FROM group_optouts WHERE scope=? AND user_id=?", (scope,user_id))

    @staticmethod
    def contains_credentials(text: str) -> bool:
        return bool(re.search(r"\bsk-[A-Za-z0-9_-]{16,}|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}",text)
            or (re.search(r"(?i:api[_ -]?key|app[_ -]?secret|access[_ -]?token|password)|密码|密钥",text)
                and re.search(r"[:：=]\s*\S{4,}",text)))

    def acceptable(self, text: str) -> bool:
        """Conservative local filter; omitted text is never counted or persisted."""
        if not isinstance(text,str) or not text.strip() or len(text) > self.max_text_chars:
            return False
        text = text.strip()
        if text.startswith(("/", "!", "！")):
            return False
        if self.contains_credentials(text):
            return False
        return not any(ord(c) < 32 and c not in "\n\t" for c in text)

    def add(self, scope: str, user_id: str, text: str, message_id: str,
            display_name: str = "", role: str = "user") -> int | None:
        """Add one trusted event; return new revision, None if filtered/duplicate.

        Add assistant/bot text only after confirmed delivery, never on generation.
        Both assistant and user events are deduplicated by scope+role+message ID.
        """
        self._id(scope); self._id(user_id); self._id(message_id)
        role = "assistant" if role == "bot" else role
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported group role")
        if not self.acceptable(text):
            return None
        name = "".join(c for c in str(display_name) if ord(c) >= 32)[:100]
        if self.contains_credentials(name):
            name="（昵称已隐藏）"
        now = self.clock()
        event_hash = hashlib.sha256((scope+"\0"+role+"\0"+message_id).encode()).hexdigest()
        with self._guard, self.db() as db:
            self._prune_memory(now)
            self._prune_persistent(db,now)
            if self._opted_out(db,scope,user_id):
                return None
            inserted = db.execute("INSERT OR IGNORE INTO group_seen VALUES (?,?,?,?)", (event_hash,scope,user_id,now)).rowcount
            if not inserted:
                return None
            if role == "user":
                db.execute("""INSERT INTO group_members VALUES (?,?,?,?,?,1)
                    ON CONFLICT(scope,user_id) DO UPDATE SET display_name=excluded.display_name,
                    last_seen=excluded.last_seen,message_count=message_count+1""", (scope,user_id,name,now,now))
                db.execute("DELETE FROM group_members WHERE scope=? AND user_id NOT IN (SELECT user_id FROM group_members WHERE scope=? ORDER BY last_seen DESC,rowid DESC LIMIT ?)",
                           (scope,scope,self.max_members))
                db.execute("DELETE FROM group_members WHERE scope NOT IN (SELECT scope FROM group_members GROUP BY scope ORDER BY MAX(last_seen) DESC,MAX(rowid) DESC LIMIT ?)", (self.max_scopes,))
            else:
                db.execute("INSERT INTO group_silence VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET since=excluded.since",(scope,now))
                db.execute("INSERT INTO group_activity VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET at=excluded.at",(scope,now))
                db.execute("DELETE FROM group_activity WHERE scope NOT IN (SELECT scope FROM group_activity ORDER BY at DESC LIMIT ?)",(self.max_scopes,))
            room = self._rooms.setdefault(scope,{"messages":deque(),"version":0})
            room["messages"].append({"user_id":user_id,"display_name":name,"message_id":message_id,
                                     "text":text.strip(),"role":role,"at":now})
            while len(room["messages"]) > self._scope_limits.get(scope,(self.max_messages,self.ttl_seconds))[0]:
                room["messages"].popleft()
            members = list(dict.fromkeys(row["user_id"] for row in reversed(room["messages"])))
            keep = set(members[:self.max_members])
            room["messages"] = deque(row for row in room["messages"] if row["user_id"] in keep)
            room["version"] = self._next()
            self._rooms.move_to_end(scope)
            while len(self._rooms) > self.max_scopes:
                self._rooms.popitem(last=False)
            self._prune_persistent(db,now)
            return room["version"]

    def snapshot(self, scope: str, limit: int = 30, ttl_seconds: float | None = None) -> list[dict]:
        self._id(scope)
        if not isinstance(limit,int) or isinstance(limit,bool) or limit < 1:
            raise ValueError("Snapshot limit must be positive")
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if not isinstance(ttl,(int,float)) or not 0 < ttl <= 7200:
            raise ValueError("Invalid snapshot TTL")
        with self._guard:
            now = self.clock()
            self._prune_memory(now)
            room = self._rooms.get(scope)
            if not room:
                return []
            max_messages,scope_ttl=self._scope_limits.get(scope,(self.max_messages,self.ttl_seconds))
            rows = [dict(row, age_seconds=max(0, int(now-row["at"]))) for row in room["messages"]
                    if now-row["at"] < min(ttl,scope_ttl)]
            return rows[-min(limit,max_messages):]

    def heat(self, scope, settings):
        """Local recent activity only; no AI request or archived member history.

        Repeated same text (including repeated emoji) counts once per member;
        one member contributes at most four messages to the heat threshold.
        A rolling ten-minute persistent cap includes unconfirmed sends.
        """
        window=min(settings.group_heat_window_seconds,settings.group_chat_context_minutes*60)
        rows=self.snapshot(scope,limit=100,ttl_seconds=window)
        seen=set(); counts={}
        for row in rows:
            if row['role']!='user':continue
            text=re.sub(r'\s+','',row['text']).casefold()
            if len(text)<2 or text=='[用户发送图片]':continue
            key=(row['user_id'],text)
            if key in seen:continue
            seen.add(key)
            counts[row['user_id']]=min(4,counts.get(row['user_id'],0)+1)
        count=sum(counts.values());members=len(counts)
        hot=count>=settings.group_heat_min_messages and members>=settings.group_heat_min_members
        with self.db() as db:
            used=db.execute('SELECT COUNT(*) FROM group_heat_replies WHERE scope=? AND at>?',(scope,self.clock()-600)).fetchone()[0]
        return {'enabled':settings.group_heat_enabled,'hot':hot,'eligible':settings.group_heat_enabled and hot and used<settings.group_heat_max_replies,
                'window_seconds':window,'messages':count,'members':members,'replies_used':used,
                'replies_limit':settings.group_heat_max_replies,'quota_window_seconds':600}

    def recent_user_event(self,scope,user_id,event_id,seconds=15):
        return any(row['role']=='user' and row['user_id']==user_id and row['message_id']==event_id
                   for row in self.snapshot(scope,limit=100,ttl_seconds=seconds))

    def version(self, scope: str) -> int:
        self._id(scope)
        with self._guard:
            self._prune_memory(self.clock())
            return self._rooms.get(scope,{}).get("version",0)

    def is_current(self, scope: str, version: int) -> bool:
        return version > 0 and self.version(scope) == version

    def is_latest_user_event(self, scope: str, user_id: str, message_id: str) -> bool:
        """An active conversation is superseded by its speaker, not the room.

        Reuse the bounded, short-lived transcript instead of retaining another
        message index. Assistant receipts and other members cannot cancel the
        speaker's candidate; erasure/expiry/eviction fails closed.
        """
        self._id(scope); self._id(user_id); self._id(message_id)
        with self._guard:
            self._prune_memory(self.clock())
            for row in reversed(self._rooms.get(scope, {}).get("messages", ())):
                if row["role"] == "user" and row["user_id"] == user_id:
                    return row["message_id"] == message_id
            return False

    async def debounced(self, scope: str, version: int, seconds: float) -> bool:
        if not 0 <= seconds <= 120:
            raise ValueError("Invalid debounce delay")
        await asyncio.sleep(seconds)
        return self.is_current(scope,version)

    def lock(self, scope: str) -> asyncio.Lock:
        self._id(scope)
        return self._locks[hash(scope) % len(self._locks)]

    def _remove_user_locked(self, db, scope, user_id):
        db.execute("DELETE FROM group_members WHERE scope=? AND user_id=?", (scope,user_id))
        db.execute("DELETE FROM group_seen WHERE scope=? AND user_id=?", (scope,user_id))
        room = self._rooms.get(scope)
        if room:
            kept = deque(row for row in room["messages"] if row["user_id"] != user_id)
            if not kept:
                self._rooms.pop(scope,None)
            elif len(kept) != len(room["messages"]):
                room.update(messages=kept,version=self._next())

    def remove_user(self, scope: str, user_id: str):
        """Erase this scope's nickname/count/context; preserve opt-out and budgets."""
        self._id(scope); self._id(user_id)
        with self._guard, self.db() as db:
            self._remove_user_locked(db,scope,user_id)

    def clear_scope(self, scope: str):
        """Clear only transient context. Spending limits survive clearing/restart."""
        self._id(scope)
        with self._guard:
            self._rooms.pop(scope,None)

    def clear_all(self):
        """Invalidate all temporary transcripts without erasing durable limits."""
        with self._guard:
            self._rooms.clear()

    def familiarity(self, scope: str, user_id: str) -> dict | None:
        self._id(scope); self._id(user_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM group_members WHERE scope=? AND user_id=?", (scope,user_id)).fetchone()
            return dict(row) if row and not self._opted_out(db,scope,user_id) else None

    def familiar_members(self, scope: str, limit: int = 20) -> list[dict]:
        self._id(scope)
        if not isinstance(limit,int) or limit < 1:
            raise ValueError("Invalid member limit")
        with self.db() as db:
            rows = db.execute("SELECT * FROM group_members WHERE scope=? ORDER BY last_seen DESC LIMIT ?", (scope,min(limit,self.max_members))).fetchall()
            return [dict(row) for row in rows if not self._opted_out(db,scope,row["user_id"])]

    @staticmethod
    def _budget_args(cooldown_seconds, hour_limit, day_limit):
        if not isinstance(cooldown_seconds,(int,float)) or not 0 <= cooldown_seconds <= 86400:
            raise ValueError("Invalid group cooldown")
        if any(not isinstance(v,int) or isinstance(v,bool) or not 1 <= v <= 10000 for v in (hour_limit,day_limit)):
            raise ValueError("Invalid group reply budgets")

    def _budget(self, db, scope, now, cooldown_seconds, hour_limit, day_limit):
        self._prune_persistent(db,now)
        rows = db.execute("SELECT at,status FROM group_replies WHERE scope=? ORDER BY at", (scope,)).fetchall()
        hourly = [row["at"] for row in rows if row["at"] > now-3600]
        daily = [row["at"] for row in rows]
        reason, retry = "", 0.0
        activity=db.execute("SELECT at FROM group_activity WHERE scope=?",(scope,)).fetchone()
        last_activity=max(rows[-1]["at"] if rows else 0,activity["at"] if activity else 0)
        if last_activity and now-last_activity < cooldown_seconds:
            reason, retry = "cooldown", last_activity+cooldown_seconds-now
        if len(hourly) >= hour_limit:
            reason, retry = "hour_budget", max(retry,hourly[-hour_limit]+3600-now)
        if len(daily) >= day_limit:
            reason, retry = "day_budget", max(retry,daily[-day_limit]+86400-now)
        if not rows and db.execute("SELECT COUNT(DISTINCT scope) FROM group_replies").fetchone()[0] >= self.max_scopes:
            reason = "scope_capacity"
            retry = max(0.0,db.execute("SELECT MIN(at) FROM group_replies").fetchone()[0]+86400-now)
        return {"allowed":not reason,"can_reply":not reason,"reason":reason,
                "hour_used":len(hourly),"day_used":len(daily),
                "remaining_hour":max(0,hour_limit-len(hourly)),"remaining_day":max(0,day_limit-len(daily)),
                "retry_after":max(0.0,retry),"last_activity":dict(rows[-1]) if rows else None}

    def budget_status(self, scope: str, *, cooldown_seconds: float = 60, hour_limit: int = 6, day_limit: int = 30) -> dict:
        self._id(scope); self._budget_args(cooldown_seconds,hour_limit,day_limit)
        with self.db() as db:
            return self._budget(db,scope,self.clock(),cooldown_seconds,hour_limit,day_limit)

    def reserve(self, scope: str, *, heat_limit: int | None = None, cooldown_seconds: float = 60, hour_limit: int = 6, day_limit: int = 30) -> str | None:
        """Atomically reserve after choosing to speak, before attempting delivery.

        No release/refund exists: uncertain delivery still consumes a slot. A
        model's decision to stay silent should never call this method.
        """
        self._id(scope); self._budget_args(cooldown_seconds,hour_limit,day_limit)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            if not self._budget(db,scope,now,cooldown_seconds,hour_limit,day_limit)["allowed"]:
                return None
            if heat_limit is not None:
                used=db.execute("SELECT COUNT(*) FROM group_heat_replies WHERE scope=? AND at>?",(scope,now-600)).fetchone()[0]
                if used>=heat_limit:return None
            reservation = uuid.uuid4().hex
            db.execute("INSERT INTO group_replies VALUES (?,?,?,'reserved')", (reservation,scope,now))
            if heat_limit is not None:
                db.execute("INSERT INTO group_heat_replies VALUES (?,?,?)",(reservation,scope,now))
            return reservation

    def mark_sent(self, reservation_id: str) -> bool:
        self._id(reservation_id)
        with self.db() as db:
            return bool(db.execute("UPDATE group_replies SET status='sent' WHERE id=?", (reservation_id,)).rowcount)

    def get_stats(self) -> dict[str,int]:
        with self._guard, self.db() as db:
            self._prune_memory(self.clock())
            self._prune_persistent(db,self.clock())
            return {"context_scopes":len(self._rooms),"context_messages":sum(len(x["messages"]) for x in self._rooms.values()),
                    "familiar_members":db.execute("SELECT COUNT(*) FROM group_members").fetchone()[0],
                    "familiar_scopes":db.execute("SELECT COUNT(DISTINCT scope) FROM group_members").fetchone()[0],
                    "optouts":db.execute("SELECT COUNT(*) FROM group_optouts").fetchone()[0],
                    "reply_reservations":db.execute("SELECT COUNT(*) FROM group_replies").fetchone()[0],
                    "collection_disabled":db.execute("SELECT value FROM group_policy WHERE key='global_optout'").fetchone()[0]}

    def backup(self, path: str | Path):
        destination = Path(path)
        if destination.resolve() == self.path.resolve():
            raise ValueError("Backup destination must differ")
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd = os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
        try:
            with self.db() as source:
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def close(self):
        with self._guard:
            self._rooms.clear()
