"""One bounded idle decision per human activity cycle; no saved group text.

The bridge asks only while QQ is online. Persistent metadata prevents replay;
prepared text lives in memory, and an uncertain send is never retried.
"""
from __future__ import annotations

from .prompts import DEFAULTS as PROMPT_DEFAULTS, prompt

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import time
import uuid

from . import lore
from .bot_control import read_control
from .providers import ProviderError
from .trajectories import references as trace_references

CST = timezone(timedelta(hours=8))


def enabled(settings, group=None):
    return bool(settings.transport == "onebot" and settings.onebot_enabled
        and settings.onebot_self_id and settings.group_chat_enabled and settings.group_idle_enabled
        and (group is None or group in settings.onebot_allowed_groups and group in settings.group_idle_groups))


def in_hours(settings, now):
    hour = datetime.fromtimestamp(now, CST).hour
    start, end = settings.group_idle_start_hour, settings.group_idle_end_hour
    return False if start == end else start <= hour < end if start < end else hour >= start or hour < end


class IdleConversation:
    def __init__(self, engine, *, clock=time.time):
        self.engine, self.clock = engine, clock
        self.pending = {}
        self.lock = asyncio.Lock()
        with engine.ledger.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS group_idle_rooms (
                    scope TEXT NOT NULL, profile_id TEXT NOT NULL,
                    last_at REAL NOT NULL, version INTEGER NOT NULL,
                    attempted INTEGER NOT NULL, event_hash TEXT NOT NULL,
                    PRIMARY KEY(scope,profile_id));
                CREATE TABLE IF NOT EXISTS group_idle_attempts (
                    id TEXT PRIMARY KEY,scope TEXT NOT NULL,profile_id TEXT NOT NULL,
                    at REAL NOT NULL,status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idle_scope_time ON group_idle_attempts(scope,at);
            """)
        # Restart/switch requires fresh human activity; it cannot replay a draft
        # or turn an old quiet group into an immediate unsolicited greeting.
        self.invalidate()

    def _prune(self, db):
        now = self.clock()
        db.execute("DELETE FROM group_idle_attempts WHERE at<?", (now-7*86400,))
        db.execute("DELETE FROM group_idle_rooms WHERE last_at<?", (now-7*86400,))
        for number, item in list(self.pending.items()):
            if now-item['at'] >= 240:
                self.pending.pop(number, None)
                db.execute("UPDATE group_idle_attempts SET status='expired' WHERE id=?", (number,))

    def invalidate(self, scope=None):
        with self.engine.ledger.db() as db:
            params = [self.engine.profile_id]
            where = "profile_id=?"
            if scope is not None:
                where += " AND scope=?"; params.append(scope)
            db.execute("UPDATE group_idle_rooms SET attempted=version WHERE " + where, params)
            db.execute("UPDATE group_idle_attempts SET status='cancelled' WHERE status IN ('generating','prepared','authorized') AND " + where, params)
            self._prune(db)
        for number, item in list(self.pending.items()):
            if scope is None or item['scope'] == scope:
                self.pending.pop(number, None)

    def observe(self, event, scope):
        settings = self.engine.settings(scope)
        if (event.transport!='onebot' or event.kind!='group' or settings.transport!='onebot'
            or not settings.onebot_enabled or event.app_id!=settings.onebot_self_id
            or event.scope_id not in settings.onebot_allowed_groups):
            return
        # Observe time even with idle participation disabled; disabled activity
        # never arms a future spontaneous message. No member ID/text is stored.
        armed=enabled(settings,event.scope_id)
        # IDs/timestamps only: opt-out and media-only messages still postpone
        # a cold-room greeting without retaining their content or sender ID.
        digest = hashlib.sha256(event.event_id.encode()).hexdigest()
        with self.engine.ledger.db() as db:
            self._prune(db)
            db.execute("""INSERT INTO group_idle_rooms VALUES (?,?,?,1,?,?)
                ON CONFLICT(scope,profile_id) DO UPDATE SET
                last_at=excluded.last_at,version=group_idle_rooms.version+1,event_hash=excluded.event_hash
                ,attempted=CASE WHEN ? THEN group_idle_rooms.attempted ELSE group_idle_rooms.version+1 END
                WHERE group_idle_rooms.event_hash<>excluded.event_hash""",
                (scope,self.engine.profile_id,self.clock(),0 if armed else 1,digest,armed))
        for number, item in list(self.pending.items()):
            if item['scope'] == scope:
                self._finish(number, 'cancelled')

    def bot_spoke(self, scope):
        with self.engine.ledger.db() as db:
            db.execute("""UPDATE group_idle_rooms SET last_at=?,
                attempted=CASE WHEN attempted=version THEN version+1 ELSE attempted END,
                version=version+1 WHERE scope=? AND profile_id=?""",
                (self.clock(), scope, self.engine.profile_id))
        for number, item in list(self.pending.items()):
            if item['scope'] == scope:
                self._finish(number, 'cancelled')

    def _finish(self, number, status):
        self.pending.pop(number, None)
        with self.engine.ledger.db() as db:
            db.execute('UPDATE group_idle_attempts SET status=? WHERE id=?', (status, number))

    def _day_start(self):
        return datetime.fromtimestamp(self.clock(), CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _valid(self, candidate, *, require_hours=True):
        engine, now = self.engine, self.clock()
        settings = engine.settings(candidate["scope"])
        if (not engine.is_active() or not enabled(settings, candidate['target_id'])
                or candidate['self_id'] != settings.onebot_self_id
                or candidate['generation'] != engine.generation(candidate['scope'])
                or candidate['power_epoch'] != read_control(engine.root)['epoch']
                or require_hours and not in_hours(settings, now)
                or now-candidate['at'] >= 240):
            return False
        with engine.ledger.db() as db:
            row = db.execute('SELECT version,last_at FROM group_idle_rooms WHERE scope=? AND profile_id=?',
                             (candidate['scope'], engine.profile_id)).fetchone()
        return bool(row and row['version'] == candidate['version']
            and now-row['last_at'] >= settings.group_idle_minutes*60)

    async def claim(self, self_id):
        engine = self.engine
        if self_id != engine.settings().onebot_self_id or not engine.is_active():
            return []
        # One decision in flight per account/profile, with no backlog of drafts.
        if self.lock.locked():
            return []
        async with self.lock:
            settings, now = engine.settings(), self.clock()
            if time.monotonic() < engine._cloud_retry_after:
                return []
            with engine.ledger.db() as db:
                self._prune(db)
                rows = [dict(row) for row in db.execute(
                    'SELECT * FROM group_idle_rooms WHERE profile_id=? AND attempted<version ORDER BY last_at LIMIT 100',
                    (engine.profile_id,))]
            candidate = None
            for row in rows:
                settings=engine.settings(row['scope'])
                group = row['scope'].rsplit(':', 1)[-1]
                if (not enabled(settings, group) or not in_hours(settings,now) or now-row['last_at'] < settings.group_idle_minutes*60
                        or not row['scope'].startswith('onebot:'+self_id+':group:')):
                    continue
                if not engine.groups.budget_status(row['scope'], **engine._group_budget(settings))['allowed']:
                    continue
                with engine.ledger.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    count = db.execute('SELECT COUNT(*) FROM group_idle_attempts WHERE scope=? AND at>=?',
                                       (row['scope'], self._day_start())).fetchone()[0]
                    if count >= settings.group_idle_daily_limit:
                        continue
                    updated = db.execute('UPDATE group_idle_rooms SET attempted=version WHERE scope=? AND profile_id=? AND version=? AND attempted<version',
                                         (row['scope'], engine.profile_id, row['version'])).rowcount
                    if not updated:
                        continue
                    number = uuid.uuid4().hex
                    db.execute('INSERT INTO group_idle_attempts VALUES (?,?,?,?,?)',
                               (number, row['scope'], engine.profile_id, now, 'generating'))
                candidate = dict(id=number, claim_token=secrets.token_hex(16), at=now,
                    scope=row['scope'], target_id=group, self_id=self_id, version=row['version'],
                    generation=engine.generation(row["scope"]), power_epoch=read_control(engine.root)['epoch'])
                break
            if candidate is None:
                return []
            number = candidate['id']
            try:
                async with engine.groups.lock(candidate['scope']), engine._capacity:
                    if not self._valid(candidate):
                        self._finish(number, 'cancelled'); return []
                    settings = engine.settings(candidate["scope"])
                    if not engine.groups.budget_status(candidate['scope'], **engine._group_budget(settings))['allowed']:
                        self._finish(number, 'budget'); return []
                    context = engine._group_snapshot(candidate['scope'], settings)
                    from .core import clean_reply
                    persona = (engine.content_root/'content/persona.md').read_text()
                    knowledge = lore.for_prompt(engine.content_root, '轻松日常闲聊')
                    refs = trace_references(knowledge=knowledge, persona=persona, group_context=context)
                    confidence_policy=engine.confidence_policy(candidate["scope"],settings)
                    refs["confidence_policy"]=confidence_policy
                    settings=settings.model_copy(update={"group_chat_confidence_threshold":confidence_policy["effective"]})
                    refs['_text_preview_allowed'] = engine._preview_context_allowed(candidate['scope'], context)
                    policy = prompt(engine.content_root,"idle",settings) + prompt(engine.content_root,"knowledge",settings)+prompt(engine.content_root,"style",settings)+prompt(engine.content_root,"persona_header",settings)+persona+prompt(engine.content_root,"persona_footer",settings)
                    messages = [{'role':'system','content':policy}, {'role':'user','content':prompt(engine.content_root,"group_header",settings)+json.dumps(
                        {'recent_group_messages':context, 'lore':knowledge, 'available_expressions':[],
                         'latest_speaker_id':self_id, 'latest_speaker_own_memories':[]}, ensure_ascii=False)}]
                    answer = await engine._complete_traced(None, messages, settings, kind='group_idle',
                        trigger='background', scope=candidate['scope'], user=self_id, refs=refs,
                        context_generation=candidate['generation'], request_text=f'冷场检查：群内至少{settings.group_idle_minutes}分钟无人发言')
                    engine.provider_success(settings)
                    if not self._valid(candidate):
                        self._finish(number, 'cancelled'); return []
                    if answer.get('_voting_invalid'):
                        self._finish(number, 'invalid_votes'); return []
                    confidence = answer.get('confidence')
                    text = clean_reply(answer.get('text','') if isinstance(answer.get('text'),str) else '')
                    if (answer.get('participate') is not True or type(confidence) not in (int,float)
                            or not settings.group_chat_confidence_threshold <= confidence <= 1 or not text or len(text)>settings.group_chat_max_reply_chars
                            or '@' in text or '[CQ:' in text):
                        self._finish(number, 'silent'); return []
                    reservation = engine.groups.reserve(candidate['scope'], **engine._group_budget(settings))
                    if not reservation:
                        self._finish(number, 'budget'); return []
                    candidate.update(text=text, reservation=reservation, status='prepared')
                    self.pending[number] = candidate
                    with engine.ledger.db() as db:
                        db.execute("UPDATE group_idle_attempts SET status='prepared' WHERE id=?", (number,))
                    return [{key:candidate[key] for key in ('id','claim_token','self_id','target_id','text')}]
            except asyncio.CancelledError:
                self._finish(number, 'interrupted')
                raise
            except Exception as error:
                if isinstance(error, ProviderError):
                    engine.provider_failure(error, settings)
                self._finish(number, 'failed')
                return []

    def authorize(self, number, token, self_id):
        item = self.pending.get(number) if isinstance(number,str) else None
        if (not item or not isinstance(token,str) or not re.fullmatch(r'[a-f0-9]{32}',token) or not secrets.compare_digest(item['claim_token'], token)
                or item['self_id'] != self_id or item['status'] != 'prepared' or not self._valid(item)):
            return False
        item['status'] = 'authorized'
        with self.engine.ledger.db() as db:
            db.execute("UPDATE group_idle_attempts SET status='authorized' WHERE id=?", (number,))
        return True

    def ack(self, number, token, self_id, status, message_id=None):
        item = self.pending.get(number) if isinstance(number,str) else None
        if (not isinstance(status,str) or status not in {'sent','failed','uncertain'} or not item or not isinstance(token,str)
                or not re.fullmatch(r'[a-f0-9]{32}',token) or not secrets.compare_digest(item['claim_token'], token) or item['self_id'] != self_id):
            return {'acknowledged':False}
        valid = self._valid(item, require_hours=False)
        if status == 'sent' and (item['status'] != 'authorized' or not isinstance(message_id,str)
                or not re.fullmatch(r'-?[0-9]{1,20}', message_id)):
            return {'acknowledged':False}
        self._finish(number, status)
        if status == 'sent' and valid:
            self.engine.groups.mark_sent(item['reservation'])
            self.engine.groups.add(item['scope'], self_id, item['text'], message_id, display_name='自己', role='assistant')
            self.bot_spoke(item['scope'])
        return {'acknowledged':True}

    def status(self):
        settings = self.engine.settings()
        with self.engine.ledger.db() as db:
            self._prune(db)
            rows = [dict(row) for row in db.execute('SELECT scope,last_at,version,attempted FROM group_idle_rooms WHERE profile_id=?', (self.engine.profile_id,))]
            counts = {row['scope']:row['n'] for row in db.execute('SELECT scope,COUNT(*) n FROM group_idle_attempts WHERE at>=? GROUP BY scope', (self._day_start(),))}
        by_scope = {row['scope']:row for row in rows}
        available = list(dict.fromkeys(settings.onebot_allowed_groups))
        groups = []
        active = False
        for group in available:
            scope = 'onebot:'+settings.onebot_self_id+':group:'+group
            selected=self.engine.settings(scope)
            if group not in settings.group_idle_groups and not selected.group_idle_enabled: continue
            group_active=enabled(selected,group) and self.engine.is_active()
            active=active or group_active
            row = by_scope.get(scope, {})
            groups.append({'group_id':group, 'last_activity_at':row.get('last_at',0.),
                'armed':group_active and row.get('attempted',0)<row.get('version',0),
                'attempts_today':counts.get(scope,0)})
        return {'enabled':active, 'timezone':'Asia/Shanghai', 'groups':groups}
