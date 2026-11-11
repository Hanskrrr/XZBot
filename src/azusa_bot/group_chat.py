"""Bounded, ephemeral group context and durable content-free participation state.

The bridge supplies trusted scope/member/message IDs. Display names and text are
untrusted conversation data, never permissions. Only public familiarity metadata,
opt-outs, event hashes and reserved/sent counters are persisted; chat text is not.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from weakref import WeakValueDictionary
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
from .message_privacy import hidden_message


OBSERVATION_KEYS = ('mentions', 'mention_metadata_available', 'mentions_everyone',
                    'reply_to_id', 'quote_status', 'quoted_message', 'response_to_message_id')


class GroupConversation:
    PEER_REPLY_LIMIT = 1

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
        self._locks = WeakValueDictionary()
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
                CREATE TABLE IF NOT EXISTS group_peer_turns (
                    scope TEXT PRIMARY KEY,epoch TEXT NOT NULL,used INTEGER NOT NULL);
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
        if hidden_message(text) or text.startswith(("/", "!", "！")):
            return False
        if self.contains_credentials(text):
            return False
        return not any(ord(c) < 32 and c not in "\n\t" for c in text)

    @staticmethod
    def observation_metadata(observation, *, scope='', max_text_chars=2000):
        """Copy bounded native evidence; text never manufactures a mention.

        This deliberately omits inferred targets, quote-origin archives and
        arbitrary fields from the current-message envelope. Missing bridge
        metadata means unknown, not a verified empty mention list.
        """
        if not isinstance(observation, dict):
            return {}
        result = {'mention_metadata_available': False, 'mentions': [], 'mentions_everyone': False}
        mentions = observation.get('mentions')
        if observation.get('mention_metadata_available') is True and isinstance(mentions, list) and len(mentions) <= 20:
            seen = set()
            for member in mentions:
                if (not isinstance(member, dict) or not isinstance(member.get('user_id'), str)
                        or not re.fullmatch(r'[1-9][0-9]{0,19}', member['user_id'])
                        or member['user_id'] in seen):
                    break
                seen.add(member['user_id'])
                name = member.get('display_name', '')
                name = ''.join(c for c in name if ord(c) >= 32)[:80] if isinstance(name, str) else ''
                if GroupConversation.contains_credentials(name):
                    name = '（昵称已隐藏）'
                name_source = member.get('name_source')
                result['mentions'].append({'user_id': member['user_id'], 'display_name': name,
                    'name_source': name_source if isinstance(name_source, str) and name_source in
                        {'group_member_info', 'recent_group', 'unavailable'} else 'unavailable',
                    'is_self': member.get('is_self') is True})
            else:
                result['mention_metadata_available'] = True
                result['mentions_everyone'] = observation.get('mentions_everyone') is True
            if not result['mention_metadata_available']:
                result['mentions'] = []
        reply_to = observation.get('reply_to_id')
        if not isinstance(reply_to, str) or not re.fullmatch(r'-?[0-9]{1,20}', reply_to):
            return result
        result.update(reply_to_id=reply_to, quote_status='unavailable')
        quote = observation.get('quoted_message')
        if not isinstance(quote, dict) or observation.get('quote_status') != 'resolved':
            return result
        expected = scope.rsplit(':', 2)[-2:] if scope else None
        body = quote.get('text')
        if (quote.get('message_id') != reply_to or not isinstance(quote.get('user_id'), str)
                or not re.fullmatch(r'[1-9][0-9]{0,19}', quote['user_id'])
                or not isinstance(quote.get('kind'), str) or quote['kind'] not in {'group', 'dm'}
                or not isinstance(quote.get('scope_id'), str)
                or expected is not None and expected != [quote.get('kind'), quote.get('scope_id')]
                or not isinstance(body, str) or hidden_message(body)
                or body.lstrip().startswith(('/', '!', '！'))
                or GroupConversation.contains_credentials(body)
                or any(ord(c) < 32 and c not in '\n\t' for c in body)):
            return result
        clean = {key: quote[key] for key in ('message_id', 'user_id', 'kind', 'scope_id')}
        clean['text'] = body[:max_text_chars]
        if len(body) > max_text_chars:
            clean['text_truncated'] = True
        if isinstance(quote.get('role'), str) and quote['role'] in {'user', 'assistant'}:
            clean['role'] = quote['role']
        if type(quote.get('sent_at')) is int and quote['sent_at'] >= 0:
            clean['sent_at'] = quote['sent_at']
        media = quote.get('media_types')
        if isinstance(media, list):
            clean['media_types'] = [item for item in media[:4] if isinstance(item, str) and item in {'image', 'record', 'video', 'file'}]
        result.update(quoted_message=clean, quote_status='resolved')
        return result

    def add(self, scope: str, user_id: str, text: str, message_id: str,
            display_name: str = "", role: str = "user", *, is_bot: bool = False, mention_only: bool = False,
            observation: dict | None = None, response_to_message_id: str | None = None) -> int | None:
        """Add one trusted event; return new revision, None if filtered/duplicate.

        Add assistant/bot text only after confirmed delivery, never on generation.
        Both assistant and user events are deduplicated by scope+role+message ID.
        A trusted peer bot retains role=user so it cannot impersonate this bot's
        confirmed assistant turns. Its marker is supplied by the account registry.
        """
        self._id(scope); self._id(user_id); self._id(message_id)
        role = "assistant" if role == "bot" else role
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported group role")
        if not isinstance(is_bot, bool):
            raise ValueError("Invalid bot identity")
        # An explicit native @self has conversational meaning without prose.
        # Retain its event marker only in the bounded ephemeral transcript.
        if mention_only and (role!='user' or is_bot or not isinstance(text,str) or text.strip()):
            raise ValueError('Invalid mention-only event')
        if not mention_only and not self.acceptable(text):
            return None
        metadata = self.observation_metadata(observation, scope=scope, max_text_chars=self.max_text_chars)
        if response_to_message_id is not None:
            self._id(response_to_message_id)
            if role != 'assistant':
                raise ValueError('Only confirmed assistant speech has a response target')
            metadata['response_to_message_id'] = response_to_message_id
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
            quote = metadata.get('quoted_message')
            if quote and self._opted_out(db, scope, quote['user_id']):
                metadata.pop('quoted_message')
                metadata['quote_status'] = 'unavailable'
            for mention in metadata.get('mentions', []):
                if self._opted_out(db, scope, mention['user_id']):
                    mention.update(display_name='', name_source='unavailable')
            inserted = db.execute("INSERT OR IGNORE INTO group_seen VALUES (?,?,?,?)", (event_hash,scope,user_id,now)).rowcount
            if not inserted:
                return None
            if role == "user" and not is_bot:
                db.execute("""INSERT INTO group_members VALUES (?,?,?,?,?,1)
                    ON CONFLICT(scope,user_id) DO UPDATE SET display_name=excluded.display_name,
                    last_seen=excluded.last_seen,message_count=message_count+1""", (scope,user_id,name,now,now))
                db.execute("DELETE FROM group_members WHERE scope=? AND user_id NOT IN (SELECT user_id FROM group_members WHERE scope=? ORDER BY last_seen DESC,rowid DESC LIMIT ?)",
                           (scope,scope,self.max_members))
                db.execute("DELETE FROM group_members WHERE scope NOT IN (SELECT scope FROM group_members GROUP BY scope ORDER BY MAX(last_seen) DESC,MAX(rowid) DESC LIMIT ?)", (self.max_scopes,))
                # Only a newly accepted human message rearms peer interaction.
                # Content expiry, receipt ACKs and bot messages never refund it.
                if self._peer_budget(db, scope)["allowed"] or db.execute(
                        "SELECT 1 FROM group_peer_turns WHERE scope=?", (scope,)).fetchone():
                    db.execute("""INSERT INTO group_peer_turns VALUES (?,?,0)
                        ON CONFLICT(scope) DO UPDATE SET epoch=excluded.epoch,used=0""",
                        (scope, uuid.uuid4().hex))
            else:
                if role == "assistant":
                    db.execute("INSERT INTO group_silence VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET since=excluded.since",(scope,now))
                    db.execute("INSERT INTO group_activity VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET at=excluded.at",(scope,now))
                    db.execute("DELETE FROM group_activity WHERE scope NOT IN (SELECT scope FROM group_activity ORDER BY at DESC LIMIT ?)",(self.max_scopes,))
                else:
                    db.execute("DELETE FROM group_members WHERE scope=? AND user_id=?", (scope,user_id))
            room = self._rooms.setdefault(scope,{"messages":deque(),"version":0})
            room["messages"].append({"user_id":user_id,"display_name":name,"message_id":message_id,
                                     "text":text.strip(),"role":role,"at":now,
                                     **({"is_bot":True} if is_bot else {}),
                                     **({'mention_only':True} if mention_only else {}), **metadata})
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
            rows = [dict(deepcopy(row), age_seconds=max(0, int(now-row["at"]))) for row in room["messages"]
                    if now-row["at"] < min(ttl,scope_ttl) and not hidden_message(row["text"])]
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
            if row['role']!='user' or row.get('is_bot'):continue
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
        lock=self._locks.get(scope)
        if lock is None:
            lock=asyncio.Lock()
            self._locks[scope]=lock
        return lock

    def _remove_user_locked(self, db, scope, user_id):
        db.execute("DELETE FROM group_members WHERE scope=? AND user_id=?", (scope,user_id))
        db.execute("DELETE FROM group_seen WHERE scope=? AND user_id=?", (scope,user_id))
        room = self._rooms.get(scope)
        if room:
            kept = deque(row for row in room["messages"] if row["user_id"] != user_id)
            changed = len(kept) != len(room['messages'])
            for row in kept:
                if row.get('quoted_message', {}).get('user_id') == user_id:
                    row.pop('quoted_message')
                    row['quote_status'] = 'unavailable'
                    changed = True
                for mention in row.get('mentions', []):
                    if mention['user_id'] == user_id and mention.get('display_name'):
                        mention.update(display_name='', name_source='unavailable')
                        changed = True
            if not kept:
                self._rooms.pop(scope,None)
            elif changed:
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

    def _peer_budget(self, db, scope):
        row = db.execute("SELECT epoch,used FROM group_peer_turns WHERE scope=?", (scope,)).fetchone()
        capacity = row is not None or db.execute("SELECT COUNT(*) FROM group_peer_turns").fetchone()[0] < self.max_scopes*4
        return {"epoch":row["epoch"] if row else "", "used":row["used"] if row else 0,
                "limit":self.PEER_REPLY_LIMIT, "allowed":capacity and (row is None or row["used"] < self.PEER_REPLY_LIMIT)}

    def peer_budget(self, scope: str) -> dict:
        """One shortcut reply per account/profile/group until human input.

        Durable counters contain no chat text, never expire and are not refunded
        after uncertain delivery. Exhaustion disables the shortcut, not ordinary
        group participation. Full capacity refuses new shortcut scopes.
        """
        self._id(scope)
        with self.db() as db:
            return self._peer_budget(db, scope)

    def reserve_peer_reply(self, scope: str, epoch: str) -> bool:
        """Reserve before send, refusing a stale generation after human input."""
        self._id(scope)
        if not isinstance(epoch, str):
            raise ValueError("Invalid peer epoch")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._peer_budget(db, scope)
            if not state["allowed"] or state["epoch"] != epoch:
                return False
            db.execute("""INSERT INTO group_peer_turns VALUES (?,?,1)
                ON CONFLICT(scope) DO UPDATE SET used=used+1""", (scope,epoch))
            return True

    def peer_epoch_current(self, scope: str, epoch: str) -> bool:
        self._id(scope)
        if not isinstance(epoch, str):
            return False
        with self.db() as db:
            row = db.execute("SELECT epoch FROM group_peer_turns WHERE scope=?", (scope,)).fetchone()
            return row["epoch"] == epoch if row else epoch == ""

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
