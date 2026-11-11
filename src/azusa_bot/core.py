from __future__ import annotations

from .prompts import DEFAULTS as PROMPT_DEFAULTS, prompt, current_revision as prompt_revision, with_persona

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ConfigDict, model_validator

from . import lore, assets, web_search, group_topic, participation_vote, conversation_config, segmented_delivery, request_context, reply_contract, peer_bots
from .memory import MemoryStore
from .message_privacy import hidden_message
from .group_chat import GroupConversation
from .dialogue import RecentDialogue, refers_back, requests_repeat
from .providers import CloudProviders, MockProviders, ProviderError, SUMMARY_OUTPUT_TOKENS
from .provider_usage import CompletionResult
from .diagnostics import Diagnostics, safe_code
from .secrets import load_credentials, resolve_credentials
from .credential_store import CredentialStoreError
from .settings import QQ_ID_PATTERN, Settings, read_settings
from .trajectories import references as trace_references, TrajectorySourceErased
from .bot_control import activity_allowed, read_control
from .group_queue import GroupQueue, QueueStopped
from .context_budget import fit_context, fit_retry, estimate_tokens
from .vision import VisionCache
from .vision_prompt import VISION_POLICY, retained_events, attach_images, visual_sources
from .group_addressing import is_addressed
from .local_conversation import LocalConversation
from . import group_planning

log = logging.getLogger("azusa")
ID = r"^[A-Za-z0-9_-]+$"
GROUP_COMMANDS = {"/帮助", "/隐私", "/身份", "/记忆", "/记住", "/群记住", "/事件", "/关系", "/忘记", "/纠正", "/忘记我", "/语音", "/文字", "/表情", "/旁听", "/订阅"}
# Requests that ended locally on purpose; reported as-is, never as a cloud failure.
LOCAL_STOP_REASONS = frozenset({"conversation_plan_changed","conversation_plan_invalid","conversation_plan_unavailable","activity_state_conflict","bot_stopped","cloud_budget","context_budget_exceeded","context_retry_budget","privacy_context_changed"})


class _GeneratedContextChanged(ProviderError):
    """Distinguish a cancelled generated answer from a pre-request withdrawal."""

    def __init__(self):
        super().__init__("privacy_context_changed")


# Shared by direct replies, passive group participation and follow-up turns.
# These model instructions reduce exposure; they are not a secrecy boundary.
KNOWLEDGE_POLICY = PROMPT_DEFAULTS["knowledge"]


EXPRESSION_POLICY = PROMPT_DEFAULTS["expression"]


def output_policy(settings, root=None):
    return prompt(root,"output",settings)


class QuotedMessage(BaseModel):
    """One explicit native reply, scoped and fetched by the authenticated bridge."""
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(max_length=21, pattern=r"^-?[0-9]{1,20}$")
    user_id: str = Field(pattern=QQ_ID_PATTERN)
    kind: Literal["group", "dm"]
    scope_id: str = Field(pattern=QQ_ID_PATTERN)
    text: str = Field(max_length=6000)
    sent_at: int = Field(ge=0)
    media_types: list[Literal["image", "record", "video", "file"]] = Field(default_factory=list, max_length=4)


class MentionedMember(BaseModel):
    """A public label resolved by the bridge for one native, same-group @ ID."""
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(pattern=QQ_ID_PATTERN)
    display_name: str = Field(default="",max_length=80)


class Incoming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["official", "onebot"] = "official"
    app_id: str = Field(min_length=1, max_length=160, pattern=ID)
    kind: Literal["group", "dm"]
    scope_id: str = Field(min_length=1, max_length=160, pattern=ID)
    user_id: str = Field(min_length=1, max_length=160, pattern=ID)
    event_id: str = Field(min_length=1, max_length=250)
    text: str = Field(max_length=10000)
    mentioned: bool = False
    other_mentioned: bool = False
    mentioned_user_ids: list[Annotated[str,Field(pattern=QQ_ID_PATTERN)]] | None = Field(default=None,max_length=20)
    mentions_everyone: bool = False
    mentioned_members: list[MentionedMember] = Field(default_factory=list,max_length=20)
    unprompted: bool = False
    display_name: str = Field(default="", max_length=80)
    reply_to_id: str | None = Field(default=None, max_length=21, pattern=r"^-?[0-9]{1,20}$")
    quoted_message: QuotedMessage | None = None
    images: list[Annotated[str,Field(max_length=8192)]] = Field(default_factory=list, max_length=3)
    activity_only: bool = False

    @property
    def mention_only(self):
        return (self.transport == 'onebot' and self.kind == 'group' and self.mentioned
                and not self.text.strip() and not self.images)

    @model_validator(mode="after")
    def validate_onebot_ids(self):
        if self.transport == "onebot" and not all(
            re.fullmatch(QQ_ID_PATTERN, value) for value in (self.app_id, self.scope_id, self.user_id)
        ):
            raise ValueError("OneBot requires numeric self, group, and user IDs")
        if self.transport != "onebot" and (self.mentioned_user_ids is not None or self.mentions_everyone or self.mentioned_members):
            raise ValueError("Native QQ mentions require OneBot")
        if self.mentioned_user_ids is None:
            if self.mentions_everyone or self.mentioned_members:
                raise ValueError("Mention labels require native mention IDs")
        else:
            ids = self.mentioned_user_ids
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate native mention IDs")
            labels = [member.user_id for member in self.mentioned_members]
            if len(labels) != len(set(labels)) or not set(labels) <= set(ids):
                raise ValueError("Mention labels must identify distinct native mentions")
            if self.mentioned_members and self.kind != "group":
                raise ValueError("Group member labels require a group event")
            self.mentioned = self.app_id in ids
            self.other_mentioned = self.mentions_everyone or any(user != self.app_id for user in ids)
        if self.quoted_message and (self.transport != "onebot" or
                self.quoted_message.message_id != self.reply_to_id or
                (self.quoted_message.kind, self.quoted_message.scope_id) != (self.kind, self.scope_id)):
            raise ValueError("Quoted message must belong to the native reply and the same conversation")
        return self


def clean_reply(text: str) -> str:
    text = re.sub(r"[（(【\[]?\s*(?:语音已发送|已发送语音|语音发送成功)[。.!！]?\s*[）)】\]]?", "", text)
    return text.strip()[:3000]


class Ledger:
    """Reserve before generation; never replay an uncertain outgoing message."""
    def __init__(self, path: Path):
        self.path = path
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS deliveries (event_key TEXT PRIMARY KEY, reply_id TEXT UNIQUE,
              scope TEXT, user_id TEXT, status TEXT, mode TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, used INTEGER);
            CREATE TABLE IF NOT EXISTS topic_checks (scope TEXT PRIMARY KEY,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS summaries (scope TEXT, user_id TEXT, through_id INTEGER,
              PRIMARY KEY(scope,user_id));
            """)
            if "media_name" not in {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}:
                db.execute("ALTER TABLE deliveries ADD COLUMN media_name TEXT")
            columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            for column in ("profile_id", "profile_epoch", "asset_reference"):
                if column not in columns:
                    db.execute(f"ALTER TABLE deliveries ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            for column in ('source_event_id', 'error_code', 'claim_token'):
                if column not in columns:
                    db.execute(f"ALTER TABLE deliveries ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            if "queue_wait_seconds" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN queue_wait_seconds REAL NOT NULL DEFAULT 0")
            if "trigger_kind" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN trigger_kind TEXT NOT NULL DEFAULT ''")
            if "retry_after_seconds" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN retry_after_seconds REAL NOT NULL DEFAULT 0")
            if "peer_bot" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN peer_bot INTEGER NOT NULL DEFAULT 0")
            if "group_target" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN group_target INTEGER NOT NULL DEFAULT 0")
            if "window_entered" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN window_entered INTEGER")
            if "decision_path" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN decision_path TEXT NOT NULL DEFAULT ''")
            if "decision_reason" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN decision_reason TEXT NOT NULL DEFAULT ''")
            if "receipt_message_id" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN receipt_message_id TEXT NOT NULL DEFAULT ''")
            if "receipt_at" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN receipt_at REAL")
            db.execute("CREATE INDEX IF NOT EXISTS deliveries_profile_page ON deliveries(profile_id, at DESC, reply_id DESC)")
            segmented_delivery.schema(db)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event_key(scope: str, event_id: str) -> str:
        return hashlib.sha256((scope + "|" + event_id).encode()).hexdigest()

    def reserve(self, event: Incoming, scope: str, profile_id: str = "default", profile_epoch: str = "") -> str | None:
        key = self.event_key(scope, event.event_id)
        reply_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute("DELETE FROM deliveries WHERE at < ?", (time.time() - 30*86400,))
            db.execute("DELETE FROM delivery_segments WHERE reply_id NOT IN (SELECT reply_id FROM deliveries)")
            cur = db.execute("INSERT OR IGNORE INTO deliveries (event_key,reply_id,scope,user_id,status,mode,at,profile_id,profile_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, reply_id, scope, event.user_id, "processing", "none", time.time(),profile_id,profile_epoch))
            if cur.rowcount:
                source=event.event_id if re.fullmatch(r'-?[0-9]{1,20}',event.event_id) else 'sha256:'+hashlib.sha256(event.event_id.encode()).hexdigest()
                trigger = ('mention' if event.mentioned else 'quote' if event.reply_to_id
                           else 'observe' if event.unprompted else 'direct')
                db.execute('UPDATE deliveries SET source_event_id=?,trigger_kind=?,window_entered=? WHERE reply_id=?',
                           (source, trigger, 0 if event.kind == 'group' else None, reply_id))
                return reply_id
            return None

    def record_intake(self, reply_id: str, path: str, *, window_entered: bool | None = None, reason: str = ''):
        with self.db() as db:
            db.execute('UPDATE deliveries SET decision_path=?,decision_reason=?,window_entered=COALESCE(?,window_entered) '
                       'WHERE reply_id=?', (path, reason, window_entered, reply_id))

    def mark(self, reply_id: str, status: str, mode: str | None = None):
        with self.db() as db:
            if mode:
                db.execute("UPDATE deliveries SET status=?,mode=? WHERE reply_id=?", (status, mode, reply_id))
            else:
                db.execute("UPDATE deliveries SET status=? WHERE reply_id=?", (status, reply_id))

    def accept_receipt(self, reply_id, status, *, claim_token=None, error_code="", message_id=None):
        receipt = (message_id if status == 'sent' and isinstance(message_id, str)
                   and re.fullmatch(r'-?[0-9]{1,20}', message_id) else '')
        values = (status, error_code, receipt, time.time() if receipt else None, reply_id)
        update = "UPDATE deliveries SET status=?,error_code=?,claim_token='',receipt_message_id=?,receipt_at=? WHERE reply_id=? "
        with self.db() as db:
            if claim_token is None:
                # Internal callers can supply a receipt for a prepared reply.
                changed = db.execute(
                    update + "AND status IN ('prepared','authorized')", values)
            else:
                # The bridge can cancel before claiming, or finish its own claim.
                changed = db.execute(
                    update + "AND ((status='authorized' AND claim_token=? AND ?!='send_authorization_denied') "
                    "OR (status='prepared' AND ?='cancelled' AND ?=''))",
                    (*values, claim_token, error_code, status, claim_token))
            return bool(changed.rowcount)

    def cloud_budget(self, limit: int) -> dict:
        """Read the same account-wide UTC counter used by consume; no mutation."""
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self.db() as db:
            row = db.execute("SELECT used FROM budget WHERE day=?", (day,)).fetchone()
        used = row[0] if row else 0
        return {"day": day, "timezone": "UTC", "used": used, "limit": limit,
                "remaining": max(0, limit-used), "resets_at": (int(now)//86400+1)*86400}

    def consume(self, limit: int) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO budget VALUES (?,0)", (day,))
            used = db.execute("SELECT used FROM budget WHERE day=?", (day,)).fetchone()[0]
            if used >= limit:
                return False
            db.execute("UPDATE budget SET used=used+1 WHERE day=?", (day,))
            return True

    def deliveries(self) -> list[dict]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT reply_id,scope,user_id,status,mode,at FROM deliveries ORDER BY at DESC LIMIT 100")]

    def consume_search(self, cloud_limit, search_limit):
        day=time.strftime("%Y-%m-%d",time.gmtime())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            keys=[(day,cloud_limit),("search:"+day,search_limit)]
            for key,limit in keys:
                row=db.execute("SELECT used FROM budget WHERE day=?",(key,)).fetchone()
                if row and row[0]>=limit:
                    return False
            for key,_ in keys:
                db.execute("INSERT INTO budget VALUES (?,1) ON CONFLICT(day) DO UPDATE SET used=used+1",(key,))
            return True

    def reserve_topic(self,scope,settings,*,bypass_interval=False):
        day=time.strftime("%Y-%m-%d",time.gmtime());now=time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM topic_checks WHERE at<?",(now-86400,))
            last=db.execute("SELECT at FROM topic_checks WHERE scope=?",(scope,)).fetchone()
            if not bypass_interval and last and now-last[0]<settings.group_topic_interval_seconds:return "rate_limit"
            used=db.execute("SELECT used FROM budget WHERE day=?",("topic:"+day+":"+scope,)).fetchone()
            if used and used[0]>=settings.group_topic_daily_limit:return "daily_limit"
            cloud=db.execute("SELECT used FROM budget WHERE day=?",(day,)).fetchone()
            if settings.provider_mode=="cloud" and (cloud[0] if cloud else 0)+2>settings.daily_cloud_requests:return "cloud_budget"
            db.execute("INSERT INTO topic_checks VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET at=excluded.at",(scope,now))
            db.execute("INSERT INTO budget VALUES (?,1) ON CONFLICT(day) DO UPDATE SET used=used+1",("topic:"+day+":"+scope,))
            return None


class Engine:
    def __init__(self, root: Path, *, profile_root: Path | None = None, profile_id="default",
                 profile_epoch="", voice_loader=None, active_guard=None):
        self.root = root
        self.content_root = profile_root or root
        self.profile_id, self.profile_epoch = profile_id, profile_epoch
        self._voice_loader, self._active_guard = voice_loader, active_guard
        data = self.content_root / "data"
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        data.chmod(0o700)
        from .host import protect
        protect(data)
        self.memory = MemoryStore(data / "memory.sqlite3")
        self.trajectories = self.memory.trajectories
        (root / "data").mkdir(parents=True,exist_ok=True,mode=0o700)
        self.ledger = Ledger(root / "data/state.sqlite3")
        self.local_conversation = LocalConversation(clock=lambda: time.monotonic())
        from .group_voice import GroupVoice
        self.group_voice = GroupVoice(self.ledger)
        if profile_id == "default":
            with self.ledger.db() as db:
                for row in db.execute("SELECT scope,user_id,through_id FROM summaries"):
                    self.memory.import_summary_checkpoint(row["scope"],row["user_id"],row["through_id"])
        self.diagnostics = Diagnostics(root / "data/state.sqlite3")
        self._cloud_failure_count = 0
        self._cloud_retry_after = 0.0
        self._cloud_failure_code = ''
        self.search_override = None
        initial_settings=read_settings(root)
        self.groups = GroupConversation(data / "group_state.sqlite3",
            max_messages=initial_settings.group_chat_context_messages,
            ttl_seconds=initial_settings.group_chat_context_minutes*60)
        self.group_queue=GroupQueue()
        with self.ledger.db() as db:
            db.execute("UPDATE deliveries SET status='queue_interrupted',mode='none' WHERE profile_id=? AND profile_epoch!=? AND status='queued'",
                       (self.profile_id,self.profile_epoch))
        self._group_decisions: dict[str, tuple[float, float]] = {}
        self._group_pending: dict[str, dict] = {}
        self._group_failures: dict[str, tuple[int, float]] = {}
        self._group_generation = 0
        self._scope_generations = {}
        self._context_changes = {}
        self._group_threads: dict[tuple[str,str],dict] = {}
        self._group_handoffs: dict[str,dict] = {}
        self.dialogue = RecentDialogue(lambda: time.monotonic())
        self._name_decisions: dict[str,float] = {}
        # Only in-flight routing metadata; message text stays in the existing
        # short-lived group transcript and is never copied into this index.
        self._named_candidates: dict[tuple[str,str],dict] = {}
        self.vision = VisionCache()
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._last: dict[tuple, float] = {}
        self._capacity = asyncio.Semaphore(3)
        self.provider_override = None
        self._activity_epoch = ContextVar("xzbot_activity_epoch", default=None)
        self._request_audit_text = ContextVar("xzbot_request_audit_text", default=None)
        self._message_event_context = ContextVar("xzbot_message_event", default=None)
        self._group_target_context = ContextVar("xzbot_group_target", default=None)
        self._execution_budget_context = ContextVar("xzbot_execution_budget", default=None)
        from .group_idle import IdleConversation
        self.idle = IdleConversation(self)
        self.prune_media()

    def settings(self, scope=None):
        settings=read_settings(self.root)
        if self._voice_loader:
            settings=Settings.model_validate(settings.model_dump() | self._voice_loader())
        from .group_policies import effective
        return effective(self.root,settings,scope)

    def generation(self, scope):
        return (self._group_generation, self._scope_generations.get(scope,0))

    def context_change_reason(self, scope, expected_generation=None):
        if expected_generation is not None and expected_generation == self.generation(scope):
            return 'source_changed'
        changes = [self._context_changes.get(key, (0, 'source_changed')) for key in (None, scope)]
        return max(changes, key=lambda item:item[0])[1]

    def confidence_policy(self, scope, settings):
        from .group_policies import curve
        return curve(settings,self.groups.silence_seconds(scope,initialize=False))

    def is_active(self):
        return (activity_allowed(self.root, self._activity_epoch.get())
                and (self._active_guard is None or self._active_guard()))

    def reset_provider_backoff(self):
        self._cloud_failure_count = 0
        self._cloud_retry_after = 0.0
        self._cloud_failure_code = ''

    def provider_failure(self, error, settings):
        if str(error) in {"bot_stopped","activity_state_conflict","conversation_plan_changed","conversation_observed","peer_round_limit"}:
            return
        code = safe_code(error)
        self.diagnostics.record("llm",code)
        if code == "privacy_context_changed":
            log.info("reply_cancelled code=%s", code)
            return
        log.warning("provider_failure code=%s",code)
        if code in {"context_budget_exceeded","cloud_budget","context_retry_budget","privacy_context_changed"}:
            return
        # A rejected plan or generated JSON concerns this answer, not service
        # availability. Never silence unrelated groups/explicit @ calls for it.
        if (code == 'conversation_plan_invalid'
            or code in {prefix+suffix for prefix in ('deepseek','gemini_balance')
                        for suffix in ('_output_not_json','_output_not_object')}):
            return
        if settings.provider_mode == "cloud":
            self._cloud_failure_count = min(5,self._cloud_failure_count+1)
            self._cloud_retry_after = time.monotonic()+min(900,30*2**self._cloud_failure_count)
            self._cloud_failure_code = code

    def _record_cloud_block(self, reply_id, result):
        if result.get('reason') != 'cloud_backoff':
            return
        with self.ledger.db() as db:
            db.execute('UPDATE deliveries SET error_code=?,retry_after_seconds=? WHERE reply_id=?',
                (getattr(self,'_cloud_failure_code',''),round(max(0,self._cloud_retry_after-time.monotonic()),1),reply_id))

    def provider_success(self, settings):
        if settings.provider_mode == "cloud":
            self.diagnostics.record("llm","ok")
            self.reset_provider_backoff()

    def prune_media(self):
        for path in (self.root / "data/media").glob("*"):
            if path.is_file() and path.stat().st_mtime < time.time() - 86400:
                path.unlink(missing_ok=True)

    def discard_generated_media(self, result):
        """Remove only an owned, generated media file from a cancelled reply."""
        value=result.get("media_path")
        if not isinstance(value,str):
            return
        path=Path(value)
        base=self.root / "data/media"
        if (path.is_absolute() and not base.is_symlink() and not path.is_symlink() and path.is_file()
                and re.fullmatch(r"[a-f0-9]{32}\.(wav|png|jpg|jpeg|gif|webp)",path.name)
                and path.resolve().parent==base.resolve()):
            path.unlink(missing_ok=True)

    def forget_person(self, scope: str, user: str):
        self.memory.forget_me(scope,user)
        self.trajectories.erase_image_sources(scope,user)
        self.groups.remove_user(scope,user)
        self.groups.set_optout(scope,user,True)
        self.clear_group_context(scope)
        with self.ledger.db() as db:
            media = db.execute("SELECT media_name FROM deliveries WHERE scope=? AND user_id=? AND profile_id IN (?,?)", (scope,user,self.profile_id,"" if self.profile_id == "default" else self.profile_id)).fetchall()
            if self.profile_id == "default":
                db.execute("DELETE FROM summaries WHERE scope=? AND user_id=?", (scope,user))
            for row in media:
                name = row[0]
                if name and Path(name).name == name:
                    (self.root / "data/media" / name).unlink(missing_ok=True)
            db.execute("UPDATE deliveries SET media_name=NULL WHERE scope=? AND user_id=? AND profile_id IN (?,?)", (scope,user,self.profile_id,"" if self.profile_id == "default" else self.profile_id))

    async def forget_person_when_idle(self, scope: str, user: str):
        lock = self._locks.setdefault((hash((scope,user)) % 64,), asyncio.Lock())
        async with lock:
            self.forget_person(scope,user)

    def providers(self, settings: Settings):
        if self.provider_override:
            return self.provider_override
        if settings.provider_mode == "mock":
            return MockProviders()
        try:
            credentials, _ = resolve_credentials(self.root, settings)
        except CredentialStoreError:
            raise ProviderError("credential_store_unavailable") from None
        if self.profile_id != "default":
            # A blank new role must never inherit another role's DSH/env voice.
            credentials.fish_reference = settings.fish_reference_id
            credentials.fish_model = settings.fish_model
        model = (settings.deepseek_model if settings.llm_provider == "deepseek" else
                 settings.gemini_model if settings.llm_provider == "gemini_balance" else settings.compatible_model)
        return CloudProviders(credentials, model,
                              settings.fish_model, self.root / "data/media",
                              protocol=settings.llm_provider, base_url=settings.llm_base_url,runtime_root=self.root)

    def _trace_start(self, settings, kind, trigger, scope, user, refs, *, related_id=None, model_override=None,
                     request_text=None,request_kind="message"):
        model = (settings.deepseek_model if settings.llm_provider == "deepseek" else
                 settings.gemini_model if settings.llm_provider == "gemini_balance" else settings.compatible_model)
        provider = "fish" if kind == "tts" else settings.llm_provider
        if kind == "tts":
            model = model_override or settings.fish_model
        if kind == "web_search":
            provider,model="deepseek","search"
        if settings.provider_mode == "mock":
            provider, model = "mock", "mock"
        if request_kind == "message" and self._request_audit_text.get() is not None:
            request_text = self._request_audit_text.get()
        event = self._message_event_context.get()
        if (event and event[:2] == (scope, user) and kind != 'summary'
                and trigger in {'direct', 'group_observation', 'followup'}):
            refs['message_event_key'] = event[2]
        if kind in {"group_decision","followup","group_idle","group_plan"}:
            refs["confidence_threshold"] = settings.group_chat_confidence_threshold
        try:
            return self.trajectories.begin(kind, trigger, scope, user, provider, model, refs, related_id=related_id,
                request_text=request_text,request_kind=request_kind,
                preview_allowed=not self.groups.opted_out(scope,user) and refs.get("_text_preview_allowed") is not False)
        except TrajectorySourceErased:
            raise ProviderError("privacy_context_changed") from None

    @staticmethod
    def _wants_search(answer,kind,settings):
        if not isinstance(answer.get("web_search"),dict):
            return False
        if kind=="reply":
            return True
        confidence=answer.get("confidence")
        return (answer.get("participate") is True and (kind!="followup" or answer.get("continuation") is True)
                and type(confidence) in (int,float) and settings.group_chat_confidence_threshold<=confidence<=1)

    async def _complete_traced(self,provider,messages,settings,**kwargs):
        kind=kwargs["kind"]
        if kind in {'group_idle','group_idle_manual'}:
            lease=self._group_target_context.set(None)
            budget_lease=self._execution_budget_context.set(None)
            try:
                return await self._complete_idle(provider,messages,settings,kwargs)
            finally:
                self._group_target_context.reset(lease)
                self._execution_budget_context.reset(budget_lease)
        if kind not in {"reply","group_decision","followup"}:
            return await self._complete_traced_once(provider,messages,settings,**kwargs)
        plan=await group_planning.prepare(self,messages,settings,kwargs)
        if plan:
            if plan['action']!='reply':
                return {'_local_observe':True,'_local_observe_reason':plan['reason'],'text':'','mode':'none','participate':False,'continuation':False}
            messages=group_planning.inject(self,messages,settings,kwargs,plan)
        group_planning.budget(self)
        messages=await group_topic.prepare(self,provider,messages,settings,kwargs)
        from . import role_activity, knowledge_lookup
        messages=role_activity.augment(self,messages,settings,kwargs)
        messages=copy.deepcopy(messages)
        messages[0]["content"] += "\n"+prompt(self.content_root,"search" if settings.web_search_enabled else "search_offline",settings)
        messages[0]['content']+='\n'+prompt(self.content_root,'knowledge_lookup',settings)
        messages[0]['content']+='\n'+prompt(self.content_root,'history_lookup',settings)
        from .execution import run_dialogue
        kwargs['execution_budget']=group_planning.budget(self)
        answer=await run_dialogue(self,provider,messages,settings,kwargs)
        if plan:
            if not self._group_target_current(plan):raise ProviderError('conversation_plan_changed')
            answer['_participation_plan']=group_planning.public_plan(plan)
        return answer

    async def _complete_idle(self,provider,messages,settings,kwargs):
        try:
            plan=await group_planning.prepare_idle(self,messages,settings,kwargs)
        except TimeoutError:
            raise ProviderError('conversation_plan_unavailable') from None
        if plan['action']!='reply':
            return {'_local_observe':True,'text':'','mode':'none','participate':False}
        messages=group_planning.inject(self,messages,settings,kwargs,plan)
        from .role_activity import augment
        messages=augment(self,messages,settings,kwargs)
        from .activity_guard import assess, repair
        budget=group_planning.budget(self)
        for attempt in range(2):
            answer=await self._complete_traced_once(provider,messages,settings,execution=budget,**kwargs)
            check=assess(self,answer,settings,kwargs,retry=attempt)
            if not check or check['status']!='conflict':
                if not self._group_target_current(plan):raise ProviderError('conversation_plan_changed')
                answer['_participation_plan']=group_planning.public_plan(plan)
                return answer
            if attempt or budget.model_calls>=budget.max_model_calls:raise ProviderError('activity_state_conflict')
            messages=repair(self,messages,settings,kwargs,check)
            kwargs.update(trace_id=None,related_id=kwargs['refs']['_trace_id'])

    async def _search_evidence(self, answer, messages, settings, kwargs):
        refs=kwargs["refs"];parent=refs.pop("_trace_id")
        messages=refs.pop("_prepared_messages")
        generation=kwargs.get("context_generation",self.generation(kwargs["scope"]))
        scope,user=kwargs["scope"],kwargs["user"]
        evidence={"status":"failed","searched_at":time.time(),"results":[]}
        search_refs=trace_references();search_refs["_text_preview_allowed"]=refs.get("_text_preview_allowed",True)
        search_id=self._trace_start(settings,"web_search",kwargs["trigger"],scope,user,search_refs,related_id=parent,
                                    request_text=kwargs.get("request_text"))
        try:
            query=web_search.public_query(answer["web_search"].get("query"))
            evidence["query"]=query
            latest=self.settings()
            if not self.is_active() or generation!=self.generation(kwargs["scope"]) or not latest.web_search_enabled:
                raise ProviderError("privacy_context_changed")
            if settings.provider_mode=="mock" and self.search_override is None:
                raise ProviderError("web_search_mock_disabled")
            if settings.provider_mode=="cloud" and not self.ledger.consume_search(settings.daily_cloud_requests,settings.web_search_daily_limit):
                raise ProviderError("web_search_budget")
            if self.search_override:
                self.trajectories.called(search_id)
                evidence=await self.search_override(query)
            else:
                credentials,_=resolve_credentials(self.root,latest)
                if not credentials.deepseek_key:
                    raise ProviderError("deepseek_not_configured")
                self.trajectories.called(search_id)
                evidence=await web_search.search(query,credentials.deepseek_key)
        except asyncio.CancelledError:
            self.trajectories.finish(search_id,"interrupted")
            raise
        except ProviderError as exc:
            evidence["status"]=str(exc)
        except CredentialStoreError:
            evidence["status"]="web_search_credentials_unavailable"
        if not self.is_active() or generation!=self.generation(kwargs["scope"]):
            self.trajectories.finish(search_id,"interrupted")
            raise ProviderError("privacy_context_changed")
        search_refs["web_search"]=web_search.provenance(evidence)
        self.trajectories.set_context(search_id,search_refs)
        self.trajectories.finish(search_id,"success" if evidence["status"] in {"success","no_results"} else "failed",
                                 error=None if evidence["status"] in {"success","no_results"} else evidence["status"])
        refs["web_search"]=web_search.provenance(evidence)
        return web_search.with_evidence(messages,evidence,final_policy=prompt(self.content_root,"search_done",settings),prefix=prompt(self.content_root,"direct_header" if kwargs["kind"]=="reply" else "group_header",settings)), evidence, search_id

    async def _complete_traced_once(self, provider, messages, settings, *, kind, trigger, scope, user,
                               refs, trace_id=None, context_generation=None, image_event_id=None, history_events=None,
                               request_text=None,request_kind="message",allow_search=False,allow_knowledge=False,allow_history=False,related_id=None,
                               execution=None,validate_plan=None):
        generation = self.generation(scope) if context_generation is None else context_generation
        planned = kind in reply_contract.CHAT_KINDS and self._group_target_context.get() is not None
        config = conversation_config.read(self.content_root)
        example_messages = conversation_config.examples(config) if kind in {"reply", "group_decision", "followup", "group_idle", "group_idle_manual"} else []
        example_tokens = estimate_tokens(example_messages) - 256
        if example_messages:
            messages = copy.deepcopy(messages)
            example_policy = prompt(self.content_root, "examples", settings)
            if example_policy not in messages[0]["content"]:
                messages[0]["content"] += "\n" + example_policy
        refs["begin_dialogs"] = {"pairs": len(example_messages) // 2, "revision": conversation_config.example_revision(config),
                                "estimated_tokens": example_tokens}
        if not planned and participation_vote.enabled(settings,kind):
            messages=copy.deepcopy(messages)
            vote_prompt=prompt(self.content_root,"participation_vote",settings)
            if vote_prompt not in messages[0]["content"]:
                messages[0]["content"] += "\n"+vote_prompt
        refs["prompt_prefixes"]={key:prompt(self.content_root,key,settings) for key in ("direct_header","group_header","followup_header","recent_header","current_header")}
        prefixes = dict(refs["prompt_prefixes"])
        contract_kind = ('group_idle_manual' if kind.startswith('group_idle') else 'reply') if planned else kind
        current_message_id = None
        if kind == 'group_plan':
            data = json.loads(messages[1]['content'][len(prefixes['group_header']):])
            current_message_id = data.get('current_message', {}).get('message_id')
            contract = reply_contract.build(settings, kind, idle_plan=current_message_id is None)
            contract_text = reply_contract.planning_instructions(contract, current_message_id)
        else:
            contract = reply_contract.build(settings, contract_kind, tools=[key for key, allowed in
                (("web_search",allow_search),("knowledge_query",allow_knowledge),("history_query",allow_history)) if allowed])
            contract_text = prompt(self.content_root,"reply_contract",settings) if contract else None
        render_request = lambda rows: request_context.render(rows,kind=kind,prefixes=prefixes,
            contract=contract,contract_text=contract_text)
        refs["reply_contract"] = contract
        if kind in reply_contract.CHAT_KINDS and ":group:" in scope:
            from .group_policies import FIELDS
            refs["effective_group_settings"] = {key:getattr(settings,key) for key in sorted(FIELDS)}
        refs["prompt_revision"]=prompt_revision(self.content_root)
        visual_events = retained_events(messages,refs,kind,image_event_id,history_events)
        if settings.vision_enabled and visual_events:
            await self.vision.wait_pending(scope,visual_events)
        candidates = self.vision.select(scope,visual_events,max_images=4) if settings.vision_enabled else []
        candidate_metadata = self.vision.metadata(scope,visual_events) if settings.vision_enabled else []
        failed_metadata = [row for row in candidate_metadata if row.get("status")!="ready"][-12:]
        # Covers DeepSeek's documented <=1024 image tokens plus source labels.
        # The other half is headroom, not an assertion about arbitrary models.
        per_image_tokens = 2048 if settings.llm_provider=="deepseek" else 8192
        label_tokens=sum(len(prompt(self.content_root,key,settings).encode("utf-8")) for key in ("earlier_image","image_label"))
        image_tokens = len(candidates)*per_image_tokens + len(failed_metadata)*1024 + (len(candidates)+len(failed_metadata))*label_tokens
        # The database retains original evidence. Only this request is reduced.
        fit_options = dict(kind=kind, window_tokens=settings.context_window_tokens,
                           output_tokens=SUMMARY_OUTPUT_TOKENS if kind == "summary" else 1200,
                           image_tokens=image_tokens, example_tokens=example_tokens, render_messages=render_request)
        prepared = fit_context(messages, refs, **fit_options)
        number = trace_id
        parent = related_id
        context_retry = False
        for attempt in (1, 2):
            cancelled = ("bot_stopped" if not self.is_active() else
                         "privacy_context_changed" if generation != self.generation(scope) else None)
            if cancelled:
                if number is not None:
                    self.trajectories.finish(number,"interrupted",error=cancelled,
                        cancellation_reason=self.context_change_reason(scope,generation) if cancelled=="privacy_context_changed" else None)
                raise ProviderError(cancelled)
            if attempt == 2 and not self.trajectories.exists(parent):
                raise ProviderError("privacy_context_changed")
            current_refs = prepared["refs"]
            if execution is not None:
                current_refs["execution"] = execution.snapshot(next_call=True)
            if image_event_id is not None and kind in {"reply", "group_decision", "followup"}:
                current_refs["delivery_event_key"] = self.ledger.event_key(scope, str(image_event_id))
            surviving = retained_events(prepared["messages"],current_refs,kind,image_event_id,history_events)
            selected = self.vision.select(scope,surviving,max_images=4) if settings.vision_enabled else []
            # Only images budgeted at entry may be introduced after trimming.
            candidate_ids = {row["image_id"] for row in candidates}
            selected = [row for row in selected if row["image_id"] in candidate_ids]
            selected_ids = {row["image_id"] for row in selected}
            metadata = self.vision.metadata(scope,surviving) if settings.vision_enabled else []
            allowed_metadata_ids = candidate_ids | {row["image_id"] for row in failed_metadata}
            metadata = [row for row in metadata if row["image_id"] in allowed_metadata_ids
                        and (row["image_id"] in selected_ids or row.get("status") != "ready")]
            sources = visual_sources(prepared["messages"],current_refs,kind,image_event_id,history_events)
            metadata = [dict(row, **sources.get(row["event_id"],{})) for row in metadata]
            current_refs["images"] = trace_references(images=metadata)["images"]
            current_refs["context_budget"] = dict(prepared["metrics"], attempt=attempt)
            ordered_messages = render_request(prepared["messages"])
            current_refs["request_layout"] = request_context.layout(ordered_messages,kind=kind)
            if current_refs["request_layout"]:
                order=current_refs["request_layout"]["order"]
                if example_messages:order.insert(1,"examples")
                if any(row.get("relation")!="current" for row in metadata):
                    order.insert(len(order)-1,"earlier_images")
            refs.clear(); refs.update(copy.deepcopy(current_refs))
            if number is None:
                number = self._trace_start(settings,kind,trigger,scope,user,current_refs,related_id=parent,
                    request_text=request_text,request_kind=request_kind)
            recorded = self.trajectories.set_context(number,current_refs)
            if kind == "summary" and request_kind == "summary_source":
                # Summary preparation can trim source rows; identify the last
                # retained member statement, never the full JSON/system prompt.
                summary_rows=json.loads(prepared["messages"][-1]["content"])
                if summary_rows:
                    self.trajectories.set_request_preview(number,summary_rows[-1]["text"],kind="summary_source")
            refs["_trace_id"] = number  # Runtime-only; never part of stored source references.
            try:
                if not self.is_active():
                    raise ProviderError("bot_stopped")
                from .history_lookup import valid as history_valid
                if generation != self.generation(scope) or not recorded or not history_valid(self.memory,scope,user,current_refs):
                    raise ProviderError("privacy_context_changed")
                if planned and not self._group_target_current(self._group_target_context.get()):
                    raise ProviderError("conversation_plan_changed")
                topic=current_refs.get("group_topic",{})
                if kind=="group_decision" and topic.get("required_for_heat") and not topic.get("injected"):
                    # The topic pass may have failed its gate, or its evidence
                    # may have been removed by final context fitting. Do not
                    # spend a second call generating a reply we cannot send.
                    self.trajectories.finish(number,"topic_blocked")
                    return {"participate":False,"_topic_blocked":True}
                if prepared["blocked"]:
                    raise ProviderError("context_budget_exceeded")
                if execution is not None and execution.model_calls >= execution.max_model_calls:
                    raise ProviderError("execution_budget_exhausted")
                if settings.provider_mode == "cloud" and not self.ledger.consume(settings.daily_cloud_requests):
                    raise ProviderError("context_retry_budget" if context_retry else "cloud_budget")
                if execution is not None:
                    execution.consume()
                provider = provider if provider is not None else self.providers(settings)
                request_messages = attach_images(ordered_messages,selected,current_refs["images"],current_event=image_event_id,labels={key:prompt(self.content_root,key,settings) for key in ("earlier_image","image_label")})
                # Examples go before actual context only at the transport boundary.
                # Budgeting, image association and retrieval keep their typed layout.
                if example_messages:
                    request_messages = [request_messages[0], *copy.deepcopy(example_messages), *request_messages[1:]]
                sent_inputs = request_context.input_messages(ordered_messages, kind=kind, prefixes=prefixes,
                    confirmed_plan_keys=current_refs.get('_confirmed_plan_keys', ()))
                self.trajectories.called(number, input_messages=sent_inputs)
                answer = await provider.complete(request_messages, **({"summary": True} if kind == "summary" else {}))
                if isinstance(answer, CompletionResult):
                    self.trajectories.record_usage(number, answer.usage)
                    answer = dict(answer)
                if not self.is_active():
                    raise ProviderError("bot_stopped")
                if generation != self.generation(scope) or not history_valid(self.memory,scope,user,current_refs):
                    raise _GeneratedContextChanged()
                if not isinstance(answer, dict):
                    raise ProviderError("invalid_reply_object")
                if planned and not self._group_target_current(self._group_target_context.get()):
                    raise ProviderError("conversation_plan_changed")
                contract_errors=reply_contract.control_errors(answer,settings,contract_kind)
                if not planned:answer=participation_vote.apply(answer,settings,kind)
                from .knowledge_lookup import query as knowledge_query
                from .history_lookup import query as history_query
                query_kind = "reply" if planned else kind
                researching=(allow_history and history_query(answer,query_kind,settings) is not None) or (allow_search and self._wants_search(answer,query_kind,settings)) or (allow_knowledge and knowledge_query(answer,query_kind,settings) is not None)
                if kind == "reply" and not researching and not clean_reply(answer.get("text", "") if isinstance(answer.get("text"), str) else ""):
                    raise ProviderError("empty_reply")
                if kind not in {"summary","group_topic","group_plan"}:
                    raw_response = answer.get("text", "") if isinstance(answer.get("text"),str) else ""
                    silent = not planned and kind in {"group_decision","followup","group_idle"} and (
                        answer.get("participate") is False or (kind == "followup" and answer.get("continuation") is False))
                    self.trajectories.record_response(number,"" if (silent and not answer.get("_participation_vote")) or researching else clean_reply(raw_response),
                        source_text=None if (silent and not answer.get("_participation_vote")) or researching else raw_response)
                    self.trajectories.finish(number,"invalid_output" if contract_errors else "research_requested" if researching else "success",
                        error="output_contract_invalid" if contract_errors else None, result=dict(answer,_contract_errors=contract_errors,
                        output_characters=len(answer.get("text", "")) if isinstance(answer.get("text"), str) else 0))
                if kind=="group_topic":
                    topic_prefix=refs["prompt_prefixes"]["group_header"]
                    refs["_topic_rows"]=json.loads(prepared["messages"][1]["content"][len(topic_prefix):])["recent_group_messages"]
                if kind=='group_plan':
                    prefix=refs['prompt_prefixes']['group_header']
                    refs['_plan_message_ids']=sorted(group_planning.evidence_ids(json.loads(prepared['messages'][1]['content'][len(prefix):])))
                    if validate_plan is not None:
                        return validate_plan(answer, refs)
                if researching:
                    refs["_prepared_messages"] = prepared["messages"]
                return answer
            except asyncio.CancelledError:
                self.trajectories.finish(number, "interrupted")
                raise
            except Exception as exc:
                if isinstance(exc, ProviderError):
                    self.trajectories.record_usage(number, exc.usage)
                code = str(exc)
                rejected_plan = validate_plan is not None and isinstance(exc, group_planning.InvalidPlan)
                if not rejected_plan:
                    status = ("interrupted" if code in {"bot_stopped","privacy_context_changed"} else
                              "cloud_budget" if code in {"cloud_budget","context_retry_budget"} else "failed")
                    self.trajectories.finish(number,status,error=None if code == "bot_stopped" else exc,
                        cancellation_reason=self.context_change_reason(scope,generation) if code=="privacy_context_changed" else None)
                if isinstance(exc, ProviderError) and attempt == 1:
                    retry = None
                    if code == "context_length_exceeded" and kind != "group_topic":
                        retry = fit_retry(prepared["messages"], prepared["refs"], **fit_options)
                        context_retry = True
                    elif (current_message_id is not None
                          and execution is not None and execution.model_calls + 1 < execution.max_model_calls):
                        # Leave room for generation if the corrected plan permits a reply.
                        feedback = exc.retry_feedback if rejected_plan else None
                        if code in {"deepseek_output_not_json", "deepseek_empty_content"}:
                            feedback = "上次输出未能解析。按本轮规划示例只输出一个完整的 JSON 对象。"
                        if feedback:
                            retry_messages = copy.deepcopy(prepared["messages"])
                            retry_messages[0]["content"] += "\n" + feedback
                            retry = fit_context(retry_messages, prepared["refs"], **fit_options)
                    if retry is not None and not retry["blocked"]:
                        prepared, parent, number = retry, number, None
                        continue
                raise
        raise ProviderError("context_length_exceeded")

    async def handle(self, event: Incoming, *, demo: bool = False) -> dict:
        # Drop before any trace, ledger, image download, activity or history write.
        if hidden_message(event.text):
            return {"mode": "none", "text": "", "reason": "message_hidden"}
        if event.quoted_message and hidden_message(event.quoted_message.text):
            event = event.model_copy(update={"quoted_message": None})
        original_text = event.text
        if event.images and not event.text.lstrip().startswith("/"):
            # Explicit transport annotation, never an invented picture caption.
            event = event.model_copy(update={"text":event.text.rstrip()+f"\n[群友发来{len(event.images)}张图片；图像内容仅在视觉附件中]"})
        state = read_control(self.root)
        if not state["enabled"]:
            return {"mode": "none", "text": "", "reason": "bot_stopped"}
        lease = self._activity_epoch.set(state["epoch"])
        audit_lease = self._request_audit_text.set(original_text)
        event_lease = self._message_event_context.set(None)
        target_lease = self._group_target_context.set(None)
        budget_lease = self._execution_budget_context.set(None)
        try:
            return await self._handle(event, demo=demo)
        finally:
            self._activity_epoch.reset(lease)
            self._request_audit_text.reset(audit_lease)
            self._message_event_context.reset(event_lease)
            self._group_target_context.reset(target_lease)
            self._execution_budget_context.reset(budget_lease)
            if event.kind == "group":
                prefix="onebot" if event.transport=="onebot" else "qq"
                scope=f"{prefix}:{event.app_id}:{event.kind}:{event.scope_id}"
                if demo:
                    scope=f"demo:{scope}" if event.transport=="onebot" else f"demo:{event.kind}:{event.scope_id}"
                if self.groups.opted_out(scope,event.user_id):
                    self.vision.remove_user(scope,event.user_id)

    async def _handle(self, event: Incoming, *, demo: bool = False) -> dict:
        self.prune_media()
        received_at = time.monotonic()
        settings = self.settings()
        peer_bot = False
        if not self.is_active():
            return {"mode":"none","text":"","reason":"profile_changed"}
        if not demo:
            if event.transport != settings.transport:
                return {"mode": "none", "text": "", "reason": "transport_not_selected"}
            if event.transport == "onebot":
                if not settings.onebot_enabled or not settings.onebot_self_id or event.app_id != settings.onebot_self_id:
                    return {"mode": "none", "text": "", "reason": "onebot_disabled_or_wrong_self"}
                if event.user_id == event.app_id:
                    return {"mode": "none", "text": "", "reason": "self_event"}
                allowed_groups, allowed_dm_users = settings.onebot_allowed_groups, settings.onebot_allowed_dm_users
            else:
                if not settings.qq_enabled or event.app_id != settings.app_id:
                    return {"mode": "none", "text": "", "reason": "qq_disabled_or_wrong_app"}
                allowed_groups, allowed_dm_users = settings.allowed_groups, settings.allowed_dm_users
            if event.kind == "group" and event.scope_id not in allowed_groups:
                return {"mode": "none", "text": "", "reason": "group_not_allowed"}
            if event.kind == "dm" and (event.user_id not in allowed_dm_users or event.scope_id != event.user_id):
                return {"mode": "none", "text": "", "reason": "dm_not_allowed"}
            if event.transport == "onebot" and event.user_id in settings.onebot_peer_bot_ids:
                if event.kind != "group" or event.text.lstrip().startswith(("/", "!", "！")):
                    return {"mode":"none","text":"","reason":"peer_bot_sender"}
                peer_bot = True
        prefix = "onebot" if event.transport == "onebot" else "qq"
        scope = f"{prefix}:{event.app_id}:{event.kind}:{event.scope_id}"
        if demo:
            scope = f"demo:{scope}" if event.transport == "onebot" else f"demo:{event.kind}:{event.scope_id}"
        if event.kind=="group":
            settings=self.settings(scope)
            self.groups.configure_scope(scope,settings.group_chat_context_messages,settings.group_chat_context_minutes*60)
            self.groups.silence_seconds(scope)
        reply_id = self.ledger.reserve(event, scope, self.profile_id, self.profile_epoch)
        if not reply_id:
            return {"mode": "none", "text": "", "reason": "duplicate_event"}
        self._message_event_context.set((scope, event.user_id, self.ledger.event_key(scope, event.event_id)))
        if not demo:
            if peer_bot:
                self.idle.peer_spoke(scope)
            else:
                self.idle.observe(event, scope)
        if event.activity_only and not event.mention_only:
            self.ledger.mark(reply_id, "activity_observed", "none")
            return {"reply_id":reply_id,"mode":"none","text":"","reason":"activity_observed"}
        group_mode = event.transport == "onebot" and event.kind == "group" and settings.group_chat_enabled
        if peer_bot and not group_mode:
            self.ledger.mark(reply_id, "group_chat_disabled", "none")
            return {"reply_id":reply_id,"scope":scope,"mode":"none","text":"","reason":"group_chat_disabled"}
        if event.unprompted and not group_mode:
            # Listening for authorized memory is independent of volunteering
            # replies. No shared transcript, model request, or reply is created.
            observed = False
            if (event.transport == "onebot" and event.kind == "group"
                and len(event.text) <= settings.max_input_chars
                and self.groups.acceptable(event.text)
                and not self.groups.opted_out(scope,event.user_id)
                and self.memory.consent(scope,event.user_id)):
                observed = self.memory.add_history(scope,event.user_id,"user",event.text,event.event_id) is not None
            reason="memory_observed" if observed else "group_chat_disabled"
            self.ledger.mark(reply_id,reason)
            return {"reply_id":reply_id,"scope":scope,"mode":"none","text":"","reason":reason}
        if (not group_mode and event.images and settings.vision_enabled and len(event.text)<=settings.max_input_chars
            and not GroupConversation.contains_credentials(event.text)
            and not event.text.lstrip().startswith("/")
            and (event.kind!="group" or not self.groups.opted_out(scope,event.user_id)
                 or event.mentioned or not event.unprompted)):
            generation=self.generation(scope)
            await self.vision.prepare(scope,event.user_id,event.event_id,event.images)
            if not self.is_active() or generation!=self.generation(scope):
                self.ledger.mark(reply_id,"privacy_context_changed","none")
                return {"reply_id":reply_id,"mode":"none","text":"","reason":"privacy_context_changed"}
        if group_mode:
            try:
                return await self._handle_group(event,scope,settings,reply_id,demo=demo,peer_bot=peer_bot)
            except QueueStopped as exc:
                with self.ledger.db() as db:
                    db.execute('UPDATE deliveries SET queue_wait_seconds=? WHERE reply_id=?',
                               (round(exc.wait_seconds,3),reply_id))
                return self._finish_group(event,scope,reply_id,
                    {"mode":"none","text":"","reason":str(exc)},demo=demo)
            except asyncio.CancelledError:
                self.ledger.mark(reply_id,'queue_interrupted','none')
                key=(scope,event.user_id)
                if self._named_candidates.get(key,{}).get("event_id")==event.event_id:
                    self._named_candidates.pop(key,None)
                raise
            except Exception:
                self.diagnostics.record("core","internal_failure")
                log.error("group_processing_failed reply=%s",reply_id)
                return self._finish_group(event,scope,reply_id,
                    {"mode":"none","text":"","reason":"group_internal_failure"},demo=demo)
        self.ledger.record_intake(reply_id, 'direct')
        identity = (scope, event.user_id)
        # A fixed set of striped locks prevents unbounded growth from arbitrary identities.
        lock_key = (hash(identity) % 64,)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock, self._capacity:
            if not self.is_active():
                self.ledger.mark(reply_id,"profile_changed","none")
                return {"reply_id":reply_id,"mode":"none","text":"","reason":"profile_changed"}
            if not demo and time.monotonic() - self._last.get(identity, 0) < settings.min_interval_seconds:
                self.ledger.mark(reply_id, "rate_limited")
                return {"reply_id": reply_id, "mode": "none", "text": ""}
            if len(self._last) > 10000:
                self._last.clear()
            self._last[identity] = time.monotonic()
            try:
                result = await self._reply(event, scope, settings, demo=demo)
            except ProviderError as exc:
                self.provider_failure(exc,settings)
                result = {"mode": "none", "text": "", "reason":str(exc) if str(exc) in LOCAL_STOP_REASONS else "cloud_failure"}
            except (ValueError, PermissionError):
                result = {"mode": "text", "text": "操作未完成，请检查指令和权限。"}
            except Exception:
                self.diagnostics.record("core","internal_failure")
                log.error("internal_failure reply=%s", reply_id)
                result = {"mode": "none", "text": "", "reason":"internal_failure"}
            if not self.is_active():
                self.discard_generated_media(result)
                result={"mode":"none","text":"","reason":"profile_changed"}
            generated=result.pop("_generated",False)
            context_text=result.pop("_context_text",result.get("text",""))
            if generated and result.get("mode") not in {"none",None}:
                self._stage_direct_dialogue(event,scope,reply_id,context_text,result["mode"],received_at)
                if demo:
                    self.acknowledge(reply_id,"sent",str(-int(reply_id[:12],16)),demo=True)
            result["reply_id"] = reply_id
            result["scope"] = scope
            if result.get("media_path"):
                with self.ledger.db() as db:
                    db.execute("UPDATE deliveries SET media_name=?,asset_reference=? WHERE reply_id=?",
                               (Path(result["media_path"]).name,json.dumps(result.pop("asset_reference",None)),reply_id))
            self.ledger.mark(reply_id, result.get("reason","quiet") if result["mode"]=="none" else "demo" if demo else "prepared", result["mode"])
            self._record_cloud_block(reply_id,result)
            segmented_delivery.plan(self, event, result, demo=demo)
            return result

    def clear_group_context(self, scope: str, *, reason="context_cleared"):
        self._context_changes[scope] = (time.monotonic(), reason)
        self.idle.invalidate(scope)
        self.dialogue.clear(scope)
        self.local_conversation.clear(scope)
        self._scope_generations[scope]=self._scope_generations.get(scope,0)+1
        self.group_queue.clear(scope)
        self.vision.clear_scope(scope)
        self._name_decisions.pop(scope,None)
        for key in list(self._named_candidates):
            if key[0]==scope:
                self._named_candidates.pop(key,None)
        with self.ledger.db() as db:
            cancelled=db.execute("SELECT media_name FROM deliveries WHERE scope=? AND profile_id=? AND profile_epoch=? AND status='prepared'",
                                 (scope,self.profile_id,self.profile_epoch)).fetchall()
            db.execute("UPDATE deliveries SET status='context_changed' WHERE scope=? AND profile_id=? AND profile_epoch=? AND status='prepared'",
                       (scope,self.profile_id,self.profile_epoch))
        for row in cancelled:
            if row[0] and Path(row[0]).name==row[0]:
                self.discard_generated_media({"media_path":str((self.root/"data/media"/row[0]).absolute())})
        self.groups.clear_scope(scope)
        self._group_decisions.pop(scope,None)
        self._group_handoffs.pop(scope,None)
        for key in list(self._group_threads):
            if key[0]==scope:
                self._group_threads.pop(key,None)
        for key, pending in list(self._group_pending.items()):
            if pending["scope"] == scope:
                self._group_pending.pop(key,None)

    def invalidate_groups(self, *, reason="settings_changed"):
        self._context_changes = {None:(time.monotonic(), reason)}
        self.idle.invalidate()
        self.dialogue.clear()
        self.local_conversation.clear()
        self._group_generation += 1
        self.group_queue.clear()
        self.vision.clear()
        self._name_decisions.clear()
        self._named_candidates.clear()
        with self.ledger.db() as db:
            cancelled=db.execute("SELECT media_name FROM deliveries WHERE profile_id=? AND profile_epoch=? AND status='prepared'",
                                 (self.profile_id,self.profile_epoch)).fetchall()
            db.execute("UPDATE deliveries SET status='context_changed' WHERE profile_id=? AND profile_epoch=? AND status='prepared'",
                       (self.profile_id,self.profile_epoch))
        for row in cancelled:
            if row[0] and Path(row[0]).name==row[0]:
                self.discard_generated_media({"media_path":str((self.root/"data/media"/row[0]).absolute())})
        self.groups.clear_all()
        self._group_decisions.clear()
        self._group_pending.clear()
        self._group_threads.clear()
        self._group_handoffs.clear()

    def _group_snapshot(self, scope: str, settings: Settings):
        return self.groups.snapshot(scope,limit=settings.group_chat_context_messages,
                                    ttl_seconds=settings.group_chat_context_minutes*60)

    @staticmethod
    def _group_budget(settings: Settings):
        return dict(cooldown_seconds=settings.group_chat_cooldown_seconds,
                    hour_limit=settings.group_chat_hourly_limit,day_limit=settings.group_chat_daily_limit)

    def _group_observation_gate(self, event, scope, settings, context, *, named=False, heated=False):
        """Use the same ordinary participation gates for humans and peer bots."""
        budget = self._group_budget(settings)
        if heated:
            budget["cooldown_seconds"] = settings.group_heat_interval_seconds
        if named:
            return "", budget, ""
        last_cutoff, last_at = self._group_decisions.get(scope, (0., 0.))
        interval = settings.group_heat_interval_seconds if heated else settings.group_chat_decision_interval_seconds
        if time.monotonic()-last_at < interval:
            return "decision_cooldown", budget, ""
        new_messages = sum(row["role"] == "user" and row["at"] > last_cutoff for row in context)
        if not heated and new_messages < settings.group_chat_min_messages and not re.search(r"[?？]", event.text):
            return "collecting_context", budget, ""
        if not self.groups.budget_status(scope, **budget)["allowed"]:
            return "group_speaking_budget", budget, ""
        if heated:
            entry = 'heat_threshold_met'
        else:
            entry = 'message_threshold_met' if new_messages >= settings.group_chat_min_messages else 'question_bypass'
        return "", budget, entry

    def _group_allowed_now(self, event: Incoming, *, demo: bool) -> bool:
        settings = self.settings(f"{event.transport}:{event.app_id}:group:{event.scope_id}")
        return self.is_active() and settings.group_chat_enabled and (demo or (
            settings.transport == "onebot" and settings.onebot_enabled and
            settings.onebot_self_id == event.app_id and event.scope_id in settings.onebot_allowed_groups))

    def _route_group_target(self, event, scope, settings, *, peer_bot=False):
        """Use only observations received by this character, never a registry."""
        observation = self._current_message(event)
        if peer_bot: observation['is_bot'] = True
        self.local_conversation.observe(scope,observation,self_id=event.app_id,
            names=getattr(self,'address_names_loader',lambda:[])(),
            recent=self._group_snapshot(scope,settings)+self._dialogue_context(event,scope,settings))
        plan = self.local_conversation.resolve_fast(scope,event.event_id)
        if plan is not None:
            self._group_target_context.set(plan)
            if plan['action'] != 'reply': return 'conversation_observed'
        return ''

    def _group_target_current(self, guard):
        if guard.get("source")=="private_single_turn":
            return time.monotonic()<guard["expires_at"] and self.generation(guard["scope"])==guard["generation"]
        if guard.get('source') in {'idle_timer','manual'}:
            return (time.monotonic() < guard['expires_at']
                and self.generation(guard['scope']) == guard['generation']
                and self.groups.version(guard['scope']) == guard['version']
                and self.idle.version(guard['scope']) == guard['idle_activity_version'])
        return not self.groups.opted_out(guard["scope"],guard["user_id"]) and self.local_conversation.valid(guard)

    def target_reply_current(self, reply_id):
        pending = self._group_pending.get(reply_id)
        return bool(pending and pending.get("target_guard")
                    and time.monotonic()-pending["created_at"] < 240
                    and self._group_target_current(pending["target_guard"]))

    def _repeats_group_bot(self, scope, text, *, peer_bot=False):
        context = self._group_snapshot(scope, self.settings(scope))
        if not peer_bot and not any(row.get("is_bot") for row in context):
            return False
        previous = [row["text"] for row in context if row["role"] == "assistant" or row.get("is_bot")]
        previous.extend(row["text"] for row in self._group_pending.values()
                        if row["scope"] == scope and time.monotonic()-row["created_at"] < 240)
        return peer_bots.repeats(text, previous)

    def _finish_group(self, event, scope, reply_id, result, *, demo=False, reservation=None, thread_kind=None, thread_id=None, peer_bot=False):
        target_guard = self._group_target_context.get()
        key=(scope,event.user_id)
        if self._named_candidates.get(key,{}).get("event_id")==event.event_id:
            self._named_candidates.pop(key,None)
        if not self.is_active():
            self.discard_generated_media(result)
            result={"mode":"none","text":"","reason":"profile_changed"}
        if result.get("mode") != "none" and target_guard and not self._group_target_current(target_guard):
            self.discard_generated_media(result)
            result={"mode":"none","text":"","reason":"group_target_changed"}
        if (result.get("mode") != "none" and (peer_bot or not requests_repeat(event.text))
            and self._repeats_group_bot(scope, result.get("_context_text", result.get("text", "")), peer_bot=peer_bot)):
            self.discard_generated_media(result)
            result={"mode":"none","text":"","reason":"repetitive_reply"}
        result.update(reply_id=reply_id,scope=scope)
        if result.get("mode") == "none":
            self.ledger.mark(reply_id,result.get("reason","quiet"),"none")
            self._record_cloud_block(reply_id,result)
            return result
        with self.ledger.db() as db:
            # Keep the guard requirement durable: missing transient state after
            # restart must never make a prepared peer reply eligible again.
            db.execute("UPDATE deliveries SET status=?,mode=?,peer_bot=?,group_target=? WHERE reply_id=?",
                       ("demo" if demo else "prepared",result["mode"],int(peer_bot),int(bool(target_guard)),reply_id))
        if result.get("media_path"):
            with self.ledger.db() as db:
                db.execute("UPDATE deliveries SET media_name=?,asset_reference=? WHERE reply_id=?",
                           (Path(result["media_path"]).name,json.dumps(result.pop("asset_reference",None)),reply_id))
        now = time.monotonic()
        self._group_pending = {k:v for k,v in self._group_pending.items() if now-v["created_at"]<240}
        if len(self._group_pending)>=256:
            self._group_pending.pop(next(iter(self._group_pending)))
        context_text = result.pop("_context_text", result.get("text", ""))
        if context_text:
            source=next((row for row in self._group_snapshot(scope,self.settings(scope))
                         if row["message_id"]==event.event_id and row["role"]=="user"),None)
            # ACK time must not turn an old incoming message into fresh context.
            user_at=now-max(0,self.groups.clock()-source["at"]) if source else None
            self._group_pending[reply_id] = dict(scope=scope,user_id=event.user_id,self_id=event.app_id,
                text=context_text,reservation=reservation,created_at=now,thread_kind=thread_kind,
                thread_id=thread_id,user_text=event.text,event_id=event.event_id,user_at=user_at,mode=result["mode"],reply_to_id=event.reply_to_id,
                mention_only=event.mention_only,observation=self._current_message(event))
            if peer_bot:
                self._group_pending[reply_id]["peer_guard"] = dict(
                    epoch=self.groups.peer_budget(scope)["epoch"], version=self.groups.version(scope),
                    generation=self.generation(scope))
            if target_guard:
                self._group_pending[reply_id]["target_guard"] = target_guard
            if demo:
                self.acknowledge(reply_id,"sent",str(-int(reply_id[:12],16)),demo=True)
        segmented_delivery.plan(self, event, result, demo=demo)
        if not demo:self.group_queue.expect_receipt(reply_id)
        return result

    def peer_reply_current(self, reply_id):
        """Recheck a staged peer reply at each bridge send authorization."""
        pending = self._group_pending.get(reply_id)
        guard = pending.get("peer_guard") if pending else None
        if not guard or time.monotonic()-pending["created_at"] >= 240:
            return False
        scope = pending["scope"]
        settings = self.settings(scope)
        return bool(self.is_active() and settings.transport == "onebot" and settings.onebot_enabled
            and settings.group_chat_enabled and settings.onebot_self_id == pending["self_id"]
            and scope.rsplit(":",1)[-1] in settings.onebot_allowed_groups
            and self.generation(scope) == guard["generation"]
            and self.groups.peer_epoch_current(scope,guard["epoch"]))

    def _dialogue_context(self,event,scope,settings):
        if event.kind=="group" and self.groups.opted_out(scope,event.user_id):
            return []
        ttl=settings.group_chat_context_minutes*60 if event.kind=="group" else 1800
        limit=min(12,settings.group_chat_context_messages) if event.kind=="group" else 12
        return self.dialogue.get(scope,event.user_id,ttl_seconds=ttl,limit=limit)

    def _stage_direct_dialogue(self,event,scope,reply_id,text,mode,received_at):
        if not text or (event.kind=="group" and self.groups.opted_out(scope,event.user_id)):
            return
        now=time.monotonic()
        self._group_pending={k:v for k,v in self._group_pending.items() if now-v["created_at"]<240}
        if len(self._group_pending)>=256:
            self._group_pending.pop(next(iter(self._group_pending)))
        self._group_pending[reply_id]=dict(scope=scope,user_id=event.user_id,self_id=event.app_id,
            text=text,mode=mode,reservation=None,created_at=now,user_at=received_at,
            user_text=event.text,event_id=event.event_id,dialogue_only=True,reply_to_id=event.reply_to_id,
            mention_only=event.mention_only,observation=self._current_message(event))

    def acknowledge(self, reply_id: str, status: str, message_id: str | None = None, *,
                    demo=False, claim_token=None, error_code=""):
        if demo or self.ledger.accept_receipt(reply_id, status, claim_token=claim_token,
                                             error_code=error_code, message_id=message_id):
            self._apply_reply_receipt(reply_id,status,message_id)

    def _apply_reply_receipt(self, reply_id, status, message_id=None):
        try:
            self._acknowledge(reply_id,status,message_id)
        finally:
            self.group_queue.settle(reply_id)

    def _acknowledge(self, reply_id: str, status: str, message_id: str | None = None):
        pending = self._group_pending.pop(reply_id,None)
        if not self.is_active():
            return
        if pending and pending.get("thread_kind")=="continue" and (status!="sent" or not message_id):
            key=(pending["scope"],pending["user_id"])
            if self._group_threads.get(key,{}).get("id")==pending.get("thread_id"):
                self._group_threads.pop(key,None)
        if not pending or status != "sent" or not message_id or time.monotonic()-pending["created_at"]>=240:
            return
        from .quote_origins import remember
        for sent_id,_ in pending.get('confirmed_segments',[(message_id,pending['text'])]):
            remember(self,pending,sent_id)
        target_guard = pending.get("target_guard")
        target_current = not target_guard or self._group_target_current(target_guard)
        if pending.get("dialogue_only") or pending.get("thread_kind") in {"start","continue"}:
            self.dialogue.add(pending,message_id)
        if pending.get("dialogue_only"):
            self.memory.add_history(pending["scope"], pending["user_id"], "assistant", pending["text"], pending["event_id"]+":reply")
            return
        if pending["reservation"]:
            self.groups.mark_sent(pending["reservation"])
        # Only an acknowledged real send (or explicit simulator send) joins the
        # shared transcript. An uncertain send is never presented as a fact.
        for sent_id, sent_text in pending.get("confirmed_segments", [(message_id, pending["text"])]):
            self.groups.add(pending["scope"],pending["self_id"],sent_text,sent_id,
                            display_name="自己",role="assistant",response_to_message_id=pending['event_id'])
            self.local_conversation.observe(pending['scope'],dict(kind='group',message_id=sent_id,
                user_id=pending['self_id'],text=sent_text,role='assistant',
                response_to_message_id=pending['event_id'],display_name='自己',
                relation_active=target_current),self_id=pending['self_id'])
        self.idle.bot_spoke(pending["scope"])
        if target_current:
            self._acknowledge_thread(pending,message_id)

    def _active_thread(self,scope,user,settings):
        now=time.monotonic()
        for key,value in list(self._group_threads.items()):
            if now>=value["expires"] or now-value["born"]>=900:
                self._group_threads.pop(key,None)
        key=(scope,user)
        thread=self._group_threads.get(key)
        if (not settings.group_followup_enabled or not thread or
            thread["turns"]>=settings.group_followup_max_turns or self.groups.opted_out(scope,user)):
            return None
        thread["messages"] = [m for m in thread["messages"]
                              if now-m["at"] < settings.group_chat_context_minutes*60]
        return thread

    def _handoff_thread(self,event,scope,settings):
        """A new member may be replying to a recent public, confirmed turn.

        Only the shared group transcript is eligible; another member's private
        dialogue or memories are never inherited. The model still decides.
        """
        from .group_handoff import candidate
        if self.groups.opted_out(scope,event.user_id):
            return None
        context=self._group_snapshot(scope,settings)
        hint=candidate(context,event.text,settings)
        if hint is None:
            return None
        # Do not reopen this member's already closed/capped conversation merely
        # because it contains a pronoun. Explicit refer-back keeps its own path.
        if any(row['message_id']==hint['anchor_message_id'] and row['role']=='assistant'
               for row in self._dialogue_context(event,scope,settings)):
            return None
        state=self._group_handoffs.get(scope)
        if not state or state['anchor_message_id']!=hint['anchor_message_id']:
            state={'anchor_message_id':hint['anchor_message_id'],'attempts':0,'last_attempt':0.,'checked':set()}
            self._group_handoffs[scope]=state
            if len(self._group_handoffs)>128:
                self._group_handoffs.pop(next(iter(self._group_handoffs)))
        if event.user_id in state['checked'] or state['attempts']>=settings.group_followup_max_turns:
            return None
        now=time.monotonic()
        anchor=next(row for row in context if row['message_id']==hint['anchor_message_id'] and row['role']=='assistant')
        thread=dict(id=uuid.uuid4().hex,born=now,
                    expires=now+max(0,settings.group_followup_handoff_seconds-hint['anchor_age_seconds']),
                    turns=0,last_attempt=state['last_attempt'],
                    messages=[dict(anchor,at=now-hint['anchor_age_seconds'])],generation_at=now,pending=None,
                    handoff=hint,handoff_state=state)
        self._group_threads[(scope,event.user_id)]=thread
        return thread

    def _handoff_current(self,event,scope,thread):
        if 'handoff' not in thread:
            return True
        from .group_handoff import candidate
        hint=candidate(self._group_snapshot(scope,self.settings(scope)),event.text,self.settings(scope),require_cue=False)
        return hint is not None and hint['anchor_message_id']==thread['handoff']['anchor_message_id']

    @staticmethod
    def _thread_context(thread):
        now=time.monotonic()
        return [dict(m,age_seconds=max(0,int(now-m["at"]))) for m in thread["messages"]]

    def _related_members(self,event,scope):
        if event.kind != 'group':return []
        candidates=list(event.mentioned_user_ids or [])
        quoted=event.quoted_message
        if quoted and not hidden_message(quoted.text):candidates.append(quoted.user_id)
        excluded={event.user_id,event.app_id}
        if event.transport == 'onebot':
            excluded.update(self.settings(scope).onebot_peer_bot_ids)
        return [user for user in dict.fromkeys(candidates) if user not in excluded
                and not self.groups.opted_out(scope,user)][:1]

    @staticmethod
    def _member_label(value):
        label="".join(char for char in value if ord(char)>=32)[:80].strip()
        return "（昵称已隐藏）" if GroupConversation.contains_credentials(label) else label

    def _current_message(self,event):
        result = dict(kind=event.kind,message_id=event.event_id,user_id=event.user_id,text=event.text,
                    has_images=bool(event.images),image_count=len(event.images),reply_to_id=event.reply_to_id)
        result["sender"]={"user_id":event.user_id,"display_name":self._member_label(event.display_name)}
        if event.mention_only:result['mention_only']=True
        scope=f'{event.transport}:{event.app_id}:{event.kind}:{event.scope_id}'
        if event.transport == 'onebot':
            result.update(bot_user_id=event.app_id,mentioned=event.mentioned,other_mentioned=event.other_mentioned,
                mention_metadata_available=event.mentioned_user_ids is not None,
                mentions_everyone=event.mentions_everyone,mentions=[])
            labels={member.user_id:self._member_label(member.display_name) for member in event.mentioned_members}
            for user in event.mentioned_user_ids or []:
                label=labels.get(user,"")
                source="group_member_info" if label else "unavailable"
                if not label and event.kind == 'group':
                    known=self.groups.familiarity(scope,user)
                    if known and known.get('display_name'):
                        label=self._member_label(known['display_name']);source="recent_group"
                result['mentions'].append(dict(user_id=user,display_name=label,name_source=source,is_self=user==event.app_id))
        if event.reply_to_id:
            result['quote_status'] = 'unavailable'
            quoted = event.quoted_message
            scope = f'{event.transport}:{event.app_id}:{event.kind}:{event.scope_id}'
            if (quoted and not hidden_message(quoted.text)
                    and not GroupConversation.contains_credentials(quoted.text)
                    and not self.groups.opted_out(scope,quoted.user_id)
                    and self.memory.collection_source(scope,quoted.user_id) != 'member_disabled'):
                result['quoted_message'] = quoted.model_dump() | {'role':'assistant' if quoted.user_id==event.app_id else 'user'}
                result['quote_status'] = 'resolved'
        if event.reply_to_id:
            from .quote_origins import resolve
            origin=resolve(self,event)
            if origin is not None:result['quote_origin']=origin
        return result

    def _acknowledge_thread(self,pending,message_id):
        kind=pending.get("thread_kind")
        scope,user=pending["scope"],pending["user_id"]
        settings=self.settings(scope)
        if kind not in {"start","continue"} or not settings.group_followup_enabled or self.groups.opted_out(scope,user):
            return
        now=time.monotonic();key=(scope,user)
        thread=self._active_thread(scope,user,settings)
        if kind=="continue":
            if not thread or thread["id"]!=pending.get("thread_id"):
                return
            thread["turns"]+=1
        else:
            if thread and thread["generation_at"]>pending["created_at"]:
                return
            thread=dict(id=uuid.uuid4().hex,born=now,expires=now,turns=0,last_attempt=0.,messages=[])
            self._group_threads[key]=thread
        thread.update(expires=min(now+settings.group_followup_seconds,thread["born"]+900),
                      generation_at=pending["created_at"],pending=None)
        # Once this member receives an answer, subsequent turns are an ordinary
        # receipt-confirmed conversation with that member.
        thread.pop('handoff',None);thread.pop('handoff_state',None)
        if pending.get("user_at") is not None:
            thread["messages"].append({"role":"user","user_id":user,"message_id":pending["event_id"],
                                       "text":pending["user_text"],"at":pending["user_at"],
                                       **({'mention_only':True} if pending.get('mention_only') else {})})
        thread["messages"].append(
            {"role":"assistant","user_id":pending["self_id"],"message_id":message_id,"text":pending["text"],"mode":pending.get("mode","text"),"at":now})
        thread["messages"]=[m for m in thread["messages"][-12:] if now-m["at"]<settings.group_chat_context_minutes*60]
        if len(self._group_threads)>256:
            self._group_threads.pop(next(iter(self._group_threads)))

    async def _continue_group_thread(self,event,scope,settings,reply_id,thread,*,demo=False):
        def quiet(reason):
            return self._finish_group(event,scope,reply_id,{"mode":"none","text":"","reason":reason},demo=demo)
        key=(scope,event.user_id);now=time.monotonic()
        context_generation = self.generation(scope)
        current=lambda: (self._group_allowed_now(event,demo=demo)
                        and context_generation==self.generation(scope)
                        and (self._group_target_current(self._group_target_context.get()) if self._group_target_context.get() else
                             self.groups.is_latest_user_event(scope,event.user_id,event.event_id) and self._handoff_current(event,scope,thread))
                        and self._active_thread(scope,event.user_id,self.settings(scope)) is thread)
        if not current():
            return quiet("followup_policy_changed")
        if thread.get("pending"):
            return quiet("followup_pending")
        # Keep the paid-call interval, but do not drop a quick follow-up. During
        # this bounded wait a newer message from the SAME member takes over;
        # ordinary activity from other members does not cancel the conversation.
        delay=min(settings.group_followup_min_interval_seconds,
                  max(0.,thread["last_attempt"]+settings.group_followup_min_interval_seconds-now))
        if delay:
            await asyncio.sleep(delay)
            if not current():
                return quiet("followup_policy_changed")
        now=time.monotonic()
        if now<self._group_failures.get(scope,(0,0.))[1] or (settings.provider_mode=="cloud" and now<self._cloud_retry_after):
            return quiet("cloud_backoff")
        context=self._group_snapshot(scope,settings)
        trace_data = {}
        if 'handoff' in thread:
            from .group_handoff import candidate
            hint=candidate(context,event.text,settings,require_cue=False)
            if hint is None:
                return quiet('followup_policy_changed')
            thread['handoff']=hint
        dialogue=self._dialogue_context(event,scope,settings)
        from .member_context import select as member_context
        own_memories = member_context(self.memory,scope,event.user_id,event.text,recent=list(context)+list(dialogue),related_users=self._related_members(event,scope))
        messages=self._group_prompt(event,scope,settings,context,trace=trace_data,memories=own_memories,dialogue=dialogue)
        messages[0]["content"] += prompt(self.content_root,"group_handoff" if 'handoff' in thread else "followup",settings)
        if 'handoff' in thread:
            trace_data['group_handoff']=dict(thread['handoff'])
        messages.append({"role":"user","content":prompt(self.content_root,"followup_header",settings)+json.dumps({
            "confirmed_dialogue":dialogue,"current_speaker":event.user_id,"latest_text":event.text,
            "group_handoff":thread.get('handoff'),
            "member_own_memories":own_memories},ensure_ascii=False)})
        trace_data["dialogue_count"] = len(dialogue)
        try:
            async with self._capacity:
                if not current():
                    return quiet("followup_policy_changed")
                # Capacity waiting is not a paid attempt. A replaced candidate
                # must not spend a request or move the next call's timestamp.
                thread["last_attempt"]=time.monotonic()
                if 'handoff_state' in thread:
                    state=thread['handoff_state']
                    if state['attempts']>=settings.group_followup_max_turns:
                        return quiet('handoff_budget')
                    state['attempts']+=1;state['last_attempt']=thread['last_attempt']
                    state['checked'].add(event.user_id)
                answer=await self._complete_traced(None,messages,settings,kind="followup",
                    trigger="followup",scope=scope,user=event.user_id,refs=trace_data,context_generation=context_generation,
                    image_event_id=event.event_id,request_text=event.text)
        except ProviderError as exc:
            if str(exc) in LOCAL_STOP_REASONS:
                self.provider_failure(exc,settings)
                return quiet("followup_policy_changed" if isinstance(exc,_GeneratedContextChanged) else str(exc))
            self.provider_failure(exc,settings)
            failures=min(5,self._group_failures.get(scope,(0,0.))[0]+1)
            self._group_failures[scope]=(failures,time.monotonic()+min(900,30*2**failures))
            return quiet("decision_unavailable")
        self.provider_success(settings)
        self._group_failures.pop(scope,None)
        if not current():
            return quiet("followup_policy_changed")
        if answer.get('_local_observe'):
            if answer.get('_local_observe_reason')=='below_participation_threshold':
                return quiet('followup_low_confidence')
            self._group_threads.pop(key,None)
            self.dialogue.end_followup(scope,event.user_id)
            return quiet('conversation_observed')
        if not answer.get('_participation_plan'):
            return quiet('conversation_plan_invalid')
        response=clean_reply(answer.get("text","") if isinstance(answer.get("text"),str) else "")
        if not response or len(response)>settings.group_chat_max_reply_chars:
            return quiet("invalid_group_reply")
        if (not requests_repeat(event.text) and any(SequenceMatcher(None,response,r["text"]).ratio()>.9 for r in thread["messages"] if r["role"]=="assistant")):
            return quiet("repetitive_reply")
        # Active conversations have their own short pacing, independent of the
        # much longer cooldown for unsolicited interjections.
        pacing=self.groups.budget_status(scope,cooldown_seconds=settings.group_followup_min_interval_seconds,hour_limit=10000,day_limit=10000)
        if pacing["reason"]=="cooldown":
            await asyncio.sleep(min(settings.group_followup_min_interval_seconds,pacing["retry_after"]))
        if not current():
            return quiet("followup_policy_changed")
        result=await self._render_reply(event,scope,settings,answer,response,trace_data,
            trigger="followup",context_generation=context_generation,valid=current)
        if result["mode"]=="none":
            return quiet(result.get("reason","followup_policy_changed"))
        thread["pending"]=reply_id
        return self._finish_group(event,scope,reply_id,result,demo=demo,
                                  thread_kind="continue",thread_id=thread["id"])

    async def _handle_peer_group(self, event, scope, settings, reply_id, *, demo=False):
        """Locally observed peer turns, bounded until another human speaks."""
        def quiet(reason):
            return self._finish_group(event,scope,reply_id,{"mode":"none","text":"","reason":reason},demo=demo,peer_bot=True)
        if len(event.text) > settings.max_input_chars:
            return quiet("observation_excluded")
        version = self.groups.add(scope,event.user_id,event.text,event.event_id,
                                  display_name=event.display_name,is_bot=True,observation=self._current_message(event))
        self.ledger.record_intake(reply_id, 'peer_observation', window_entered=version is not None)
        if version is None:
            return quiet("observation_excluded")
        target_reason=self._route_group_target(event,scope,settings,peer_bot=True)
        if target_reason:return quiet(target_reason)
        if not settings.group_chat_peer_echo_enabled or not self.groups.peer_budget(scope)['allowed']:
            return quiet('peer_round_limit')
        generation = self.generation(scope)
        state = self.groups.peer_budget(scope)
        epoch = state["epoch"]
        current = lambda: (self._group_allowed_now(event,demo=demo)
                           and generation == self.generation(scope)
                           and self.groups.peer_epoch_current(scope,epoch)
                           and (not self._group_target_context.get() or self._group_target_current(self._group_target_context.get())))
        if event.images and settings.vision_enabled:
            await self.vision.prepare(scope,event.user_id,event.event_id,event.images)
            if not current():
                return quiet("privacy_context_changed")
        context = self._group_snapshot(scope,settings)
        quoted_self = bool(event.reply_to_id and (
            any(row["role"] == "assistant" and row["message_id"] == event.reply_to_id for row in context)
            or (event.quoted_message and event.quoted_message.user_id == event.app_id)))
        async with self.groups.lock(scope):
            if not current():
                return quiet("context_changed_during_generation")
            if (time.monotonic() < self._group_failures.get(scope,(0,0.))[1]
                or (settings.provider_mode == "cloud" and time.monotonic() < self._cloud_retry_after)):
                return quiet("cloud_backoff")
            shortcut = settings.group_chat_peer_echo_enabled and self.groups.peer_budget(scope)["allowed"]
            context = self._group_snapshot(scope,settings)
            heat = self.groups.heat(scope,settings)
            heated = not shortcut and heat["eligible"] and self.groups.recent_user_event(scope,event.user_id,event.event_id)
            if shortcut:
                entry = 'peer_turn_available'
                budget = self._group_budget(settings)
                budget["cooldown_seconds"] = settings.group_followup_min_interval_seconds
                pacing = self.groups.budget_status(scope,**budget)
                if pacing["reason"] == "cooldown":
                    await asyncio.sleep(min(pacing["retry_after"],settings.group_followup_min_interval_seconds))
            else:
                reason, budget, entry = self._group_observation_gate(event,scope,settings,context,heated=heated)
                if reason:
                    return quiet(reason)
            if not current():
                return quiet("context_changed_during_generation")
            if not self.groups.budget_status(scope,**budget)["allowed"]:
                return quiet("group_speaking_budget")
            self.ledger.record_intake(reply_id, 'peer_observation', reason=entry)
            last_cutoff, _ = self._group_decisions.get(scope,(0.,0.))
            decision_started = time.monotonic()
            def candidate_current(*, check_heat_budget=False):
                if not current():
                    return False
                if shortcut:
                    return True
                if (time.monotonic()-decision_started > (15 if heated else 45)
                    or not self.groups.recent_user_event(scope,event.user_id,event.event_id)):
                    return False
                return not heated or not check_heat_budget or self.groups.heat(scope,settings)["eligible"]
            if not shortcut:
                self._group_decisions[scope] = (time.time(),decision_started)
                if len(self._group_decisions)>128:
                    self._group_decisions.pop(next(iter(self._group_decisions)))
            try:
                async with self._capacity:
                    if not current():
                        return quiet("context_changed_during_generation")
                    trace_data = {}
                    if not shortcut:
                        policy = self.confidence_policy(scope,settings)
                        trace_data["confidence_policy"] = policy
                        settings = settings.model_copy(update={"group_chat_confidence_threshold":policy["effective"]})
                    context = self._group_snapshot(scope,settings)
                    messages = self._group_prompt(event,scope,settings,context,trace=trace_data,
                                                  memories=[],dialogue=[],peer_bot=True,heat=heat if heated else None)
                    answer = await self._complete_traced(None,messages,settings,kind="group_decision",
                        trigger="group_observation",scope=scope,user=event.user_id,refs=trace_data,
                        context_generation=generation,image_event_id=event.event_id,request_text=event.text)
            except ProviderError as exc:
                self.provider_failure(exc,settings)
                if str(exc) in LOCAL_STOP_REASONS:
                    return quiet(str(exc))
                failures = min(5,self._group_failures.get(scope,(0,0.))[0]+1)
                self._group_failures[scope] = (failures,time.monotonic()+min(900,30*2**failures))
                return quiet("decision_unavailable")
            except (ValueError,TypeError):
                return quiet("decision_unavailable")
            if answer.get("_topic_blocked") is True:
                return quiet("heat_topic_not_ready")
            self.provider_success(settings)
            self._group_failures.pop(scope,None)
            if not candidate_current(check_heat_budget=True):
                if not shortcut and self._group_allowed_now(event,demo=demo) and generation == self.generation(scope):
                    self._group_decisions[scope] = (last_cutoff,decision_started)
                return quiet("context_changed_during_generation")
            if answer.get('_local_observe'):
                return quiet('low_confidence' if answer.get('_local_observe_reason')=='below_participation_threshold' else 'conversation_observed')
            if not answer.get('_participation_plan'):
                return quiet('conversation_plan_invalid')
            response = clean_reply(answer.get("text", "") if isinstance(answer.get("text"),str) else "")
            if not response or len(response) > settings.group_chat_max_reply_chars:
                return quiet("invalid_group_reply")
            if self._repeats_group_bot(scope,response,peer_bot=True):
                return quiet("repetitive_reply")
            # Every peer reply spends this character's local round allowance.
            if not self.groups.reserve_peer_reply(scope,epoch):
                return quiet("context_changed_during_generation")
            reservation = self.groups.reserve(scope,heat_limit=settings.group_heat_max_replies if heated else None,**budget)
            if not reservation:
                return quiet("group_speaking_budget")
            result = await self._render_reply(event,scope,settings,answer,response,trace_data,
                trigger="group_observation",context_generation=generation,valid=candidate_current)
            if not candidate_current():
                self.discard_generated_media(result)
                return quiet("context_changed_during_generation")
            # No human dialogue window or long-term member memory is created.
            return self._finish_group(event,scope,reply_id,result,demo=demo,
                                      reservation=reservation,peer_bot=True)

    async def _handle_group(self, event, scope, settings, reply_id, *, demo=False, peer_bot=False):
        if peer_bot:
            return await self._handle_peer_group(event,scope,settings,reply_id,demo=demo)
        def quiet(reason):
            return self._finish_group(event,scope,reply_id,{"mode":"none","text":"","reason":reason},demo=demo)
        command = event.text.strip().split(maxsplit=1)[0] if event.text.strip() else ""
        if event.text.strip() in {"/旁听 关闭","/记忆 关闭","/忘记我"}:
            # Privacy withdrawal must invalidate an in-flight decision now, not
            # queue behind a potentially slow cloud call holding the group lock.
            self.ledger.record_intake(reply_id, 'command')
            owner=settings.is_owner(event.kind,event.scope_id,event.user_id,event.transport)
            answer=self.command(event,scope,event.text.strip(),owner)
            identity=(scope,event.user_id)
            if not demo and time.monotonic()-self._last.get(identity,0)<settings.min_interval_seconds:
                return quiet("rate_limited")
            self._last[identity]=time.monotonic()
            return self._finish_group(event,scope,reply_id,{"mode":"text","text":answer},demo=demo)
        arrival_generation=self.generation(scope)
        previous = self._group_snapshot(scope,settings)
        quoted_bot = bool(event.reply_to_id and (any(r["role"]=="assistant" and r["message_id"]==event.reply_to_id for r in previous)
                         or (event.quoted_message and event.quoted_message.user_id==event.app_id)))
        direct = not event.unprompted or event.mentioned or command in GROUP_COMMANDS or quoted_bot
        addressed_elsewhere=False
        named = (not direct and not addressed_elsewhere
                 and is_addressed(event.text,getattr(self,"address_names_loader",lambda:[])()))
        now=time.monotonic();name_key=(scope,event.user_id)
        self._named_candidates={key:value for key,value in self._named_candidates.items()
            if value["expires"]>now and value["generation"]==self.generation(key[0])}
        candidate=self._named_candidates.get(name_key)
        inherited_name=(not direct and not addressed_elsewhere and candidate is not None
            and any(row["user_id"]==event.user_id and row["role"]=="user"
                    and row["message_id"]==candidate["source_event_id"] for row in previous))
        if direct or addressed_elsewhere:
            self._named_candidates.pop(name_key,None)
        elif named or inherited_name:
            # A same-member burst such as '小z' then '帮我看看这个' retains its
            # pending name cue. Completion clears it; only a confirmed send
            # can create the longer-lived conversation window.
            self._named_candidates[name_key]=dict(event_id=event.event_id,
                source_event_id=event.event_id if named else candidate["source_event_id"],
                generation=self.generation(scope),expires=now+120 if named else candidate["expires"])
            named=True
            if len(self._named_candidates)>256:
                self._named_candidates.pop(next(iter(self._named_candidates)))
        if addressed_elsewhere:
            self._group_threads.pop((scope,event.user_id),None)
            self.dialogue.end_followup(scope,event.user_id)
        version = None
        target_reason = ""
        # Commands and suspected credentials never enter the shared transcript.
        if command not in GROUP_COMMANDS and len(event.text)<=settings.max_input_chars:
            version = self.groups.add(scope,event.user_id,event.text,event.event_id,display_name=event.display_name,
                                      mention_only=event.mention_only,observation=self._current_message(event))
            if version is not None:
                target_reason = self._route_group_target(event,scope,settings)
                plan=self._group_target_context.get()
                direct = direct or bool(plan and plan["action"]=="reply" and plan["target"]=="self")
                addressed_elsewhere = bool(target_reason)
        if target_reason:
            path = 'local_plan'
        elif command in GROUP_COMMANDS and command not in {'/文字', '/语音'}:
            path = 'command'
        else:
            path = 'named' if named else 'direct' if direct else 'group_observation'
        self.ledger.record_intake(reply_id, path, window_entered=version is not None)
        if (command not in GROUP_COMMANDS and event.images and settings.vision_enabled and (version is not None or direct)
            and len(event.text)<=settings.max_input_chars and not GroupConversation.contains_credentials(event.text)
            and not event.text.lstrip().startswith("/")):
            privacy_generation=self.generation(scope)
            await self.vision.prepare(scope,event.user_id,event.event_id,event.images)
            if privacy_generation!=self.generation(scope) or not self._group_allowed_now(event,demo=demo):
                return quiet("privacy_context_changed")
        if version is not None and event.text.strip():
            self.memory.add_history(scope,event.user_id,"user",event.text,event.event_id)
        if target_reason:
            self._group_threads.pop((scope,event.user_id),None)
            self.dialogue.end_followup(scope,event.user_id)
            return quiet(target_reason)
        if addressed_elsewhere:
            return quiet("followup_not_for_bot")
        if not direct and version is None:
            return quiet("observation_excluded")
        self.ledger.mark(reply_id,'queued','none')
        async with self.group_queue.turn(scope,event.user_id,reply_id,replaceable=not direct and not named,
                                         lock=self.groups.lock(scope)) as queued:
            with self.ledger.db() as db:
                db.execute("UPDATE deliveries SET status='processing',queue_wait_seconds=? WHERE reply_id=?",
                           (round(queued.wait_seconds,3),reply_id))
            if arrival_generation!=self.generation(scope):
                return quiet('queue_context_changed')
            if not direct and not named and settings.group_chat_debounce_seconds:
                await asyncio.sleep(settings.group_chat_debounce_seconds)
            if not self._group_allowed_now(event,demo=demo):
                return quiet("group_policy_changed")
            target_guard = self._group_target_context.get()
            if target_guard and not self._group_target_current(target_guard):
                return quiet("group_target_changed")
            heat=self.groups.heat(scope,settings)
            heated=not named and not direct and not addressed_elsewhere and heat["eligible"] and self.groups.recent_user_event(scope,event.user_id,event.event_id)
            # Re-read after debounce AND lock acquisition: an earlier reply's
            # ACK may have opened this member's conversation while we waited.
            thread=self._active_thread(scope,event.user_id,self.settings(scope)) if not direct else None
            if thread and 'handoff' in thread and not self._handoff_current(event,scope,thread):
                self._group_threads.pop((scope,event.user_id),None)
                thread=None
            if not direct and not named and thread is None:
                thread=self._handoff_thread(event,scope,settings)
            # An explicit short reference can reopen a *candidate* dialogue
            # within the live context window, even after the automatic turn cap.
            # Semantic continuation and participation are still judged downstream.
            if (not direct and thread is None and settings.group_followup_enabled
                and self.dialogue.may_resume(scope,event.user_id) and refers_back(event.text)):
                recent=self._dialogue_context(event,scope,settings)
                if recent:
                    now=time.monotonic()
                    thread=dict(id=uuid.uuid4().hex,born=now,expires=now+settings.group_followup_seconds,
                        turns=0,last_attempt=0.,messages=recent,generation_at=now,pending=None)
                    self._group_threads[(scope,event.user_id)]=thread
            if not direct and not named:
                latest=(self.groups.is_latest_user_event(scope,event.user_id,event.event_id)
                        and self.groups.recent_user_event(scope,event.user_id,event.event_id))
                if not latest:
                    return quiet("queue_merged")
            if named and self.groups.opted_out(scope,event.user_id):
                return quiet("observation_excluded")
            if direct:
                privacy_generation=self.generation(scope)
                identity=(scope,event.user_id)
                if named:
                    interval=settings.group_followup_min_interval_seconds
                    pacing=self.groups.budget_status(scope,cooldown_seconds=interval,hour_limit=10000,day_limit=10000)
                    delay=min(interval,max(0.,self._name_decisions.get(scope,0.)+interval-time.monotonic(),
                        pacing['retry_after'] if pacing['reason']=='cooldown' else 0.))
                    if delay:await asyncio.sleep(delay)
                    if (privacy_generation!=self.generation(scope) or not self._group_allowed_now(event,demo=demo)
                            or self.groups.opted_out(scope,event.user_id)
                            or self._group_target_context.get() and not self._group_target_current(self._group_target_context.get())):
                        return quiet('conversation_plan_changed')
                    self._name_decisions[scope]=time.monotonic()
                    if len(self._name_decisions)>128:self._name_decisions.pop(next(iter(self._name_decisions)))
                if not demo and time.monotonic()-self._last.get(identity,0)<settings.min_interval_seconds:
                    return quiet("rate_limited")
                self._last[identity]=time.monotonic()
                dialogue=self._dialogue_context(event,scope,settings)
                self._group_threads.pop(identity,None)
                try:
                    member_lock=self._locks.setdefault((hash(identity)%64,),asyncio.Lock())
                    async with member_lock,self._capacity:
                        result=await self._reply(event,scope,settings,demo=demo,group_context=self._group_snapshot(scope,settings),group_thread=dialogue)
                except ProviderError as exc:
                    self.provider_failure(exc,settings)
                    result={"mode":"none","text":"","reason":str(exc) if str(exc) in LOCAL_STOP_REASONS else "cloud_failure"}
                except (ValueError,PermissionError):
                    result={"mode":"text","text":"操作未完成，请检查指令和权限。"}
                if not self._group_allowed_now(event,demo=demo):
                    return quiet("group_policy_changed")
                if command not in GROUP_COMMANDS and privacy_generation!=self.generation(scope):
                    return quiet("privacy_context_changed")
                self._group_decisions[scope]=(time.time(),time.monotonic())
                generated=result.pop("_generated",False)
                return self._finish_group(event,scope,reply_id,result,demo=demo,thread_kind="start" if generated else None)
            if thread:
                self.ledger.record_intake(reply_id, 'followup')
                return await self._continue_group_thread(event,scope,settings,reply_id,thread,demo=demo)
            last_cutoff,_=self._group_decisions.get(scope,(0.,0.))
            if time.monotonic()<self._group_failures.get(scope,(0,0.))[1] or (settings.provider_mode=="cloud" and time.monotonic()<self._cloud_retry_after):
                return quiet("cloud_backoff")
            context_generation = self.generation(scope)
            named_current=lambda: (self._group_allowed_now(event,demo=demo)
                and context_generation==self.generation(scope)
                and self.groups.is_latest_user_event(scope,event.user_id,event.event_id)
                and self._named_candidates.get(name_key,{}).get("event_id")==event.event_id
                and self._named_candidates.get(name_key,{}).get("expires",0)>time.monotonic()
                and not self.groups.opted_out(scope,event.user_id))
            if named:
                # A name is a request to engage, not an unsolicited interjection.
                # Keep short paid-call/delivery pacing but wait instead of losing
                # the request; newer messages by this member coalesce during it.
                interval=settings.group_followup_min_interval_seconds
                pacing=self.groups.budget_status(scope,cooldown_seconds=interval,hour_limit=10000,day_limit=10000)
                delay=min(interval,max(0.,self._name_decisions.get(scope,0.)+interval-time.monotonic(),
                    pacing["retry_after"] if pacing["reason"]=="cooldown" else 0.))
                if delay:
                    await asyncio.sleep(delay)
                if not named_current():
                    return quiet("superseded")
            context=self._group_snapshot(scope,settings)
            reason, budget, entry = self._group_observation_gate(event,scope,settings,context,named=named,heated=heated)
            if heated or entry:
                self.ledger.record_intake(reply_id, 'heated' if heated else 'group_observation', reason=entry)
            if reason:
                return quiet(reason)
            decision_started=time.monotonic()
            if not named:
                self._group_decisions[scope]=(time.time(),decision_started)
                if len(self._group_decisions)>128:
                    self._group_decisions.pop(next(iter(self._group_decisions)))
            try:
                async with self._capacity:
                    if named:
                        # Waiting for another request's capacity is not a paid
                        # attempt. Do not spend on a replaced/revoked candidate.
                        if not named_current():
                            return quiet("superseded")
                        decision_started=time.monotonic()
                        self._name_decisions[scope]=decision_started
                        self._group_decisions[scope]=(time.time(),decision_started)
                        if len(self._name_decisions)>128:
                            self._name_decisions.pop(next(iter(self._name_decisions)))
                        if len(self._group_decisions)>128:
                            self._group_decisions.pop(next(iter(self._group_decisions)))
                        context=self._group_snapshot(scope,settings)
                    if heated and not self.groups.recent_user_event(scope,event.user_id,event.event_id):
                        return quiet("superseded")
                    trace_data = {}
                    if not named:
                        policy=self.confidence_policy(scope,settings)
                        trace_data["confidence_policy"]=policy
                        settings=settings.model_copy(update={"group_chat_confidence_threshold":policy["effective"]})
                    messages = self._group_prompt(event,scope,settings,context,trace=trace_data,addressed=named,heat=heat if heated else None)
                    answer=await self._complete_traced(None,messages,settings,kind="group_decision",
                        trigger="group_observation",scope=scope,user=event.user_id,refs=trace_data,context_generation=context_generation,
                        image_event_id=event.event_id,request_text=event.text)
            except ProviderError as exc:
                if str(exc) in LOCAL_STOP_REASONS:
                    self.provider_failure(exc,settings)
                    if (str(exc)=='conversation_plan_changed' and self._group_allowed_now(event,demo=demo)
                            and context_generation==self.generation(scope)):
                        self._group_decisions[scope]=(last_cutoff,decision_started)
                    return quiet("context_changed_during_generation" if isinstance(exc,_GeneratedContextChanged) else str(exc))
                self.provider_failure(exc,settings)
                failures=min(5,self._group_failures.get(scope,(0,0.))[0]+1)
                self._group_failures[scope]=(failures,time.monotonic()+min(900,30*2**failures))
                if len(self._group_failures)>128:
                    self._group_failures.pop(next(iter(self._group_failures)))
                return quiet("decision_unavailable")
            except (ValueError,TypeError):
                return quiet("decision_unavailable")
            if answer.get("_topic_blocked") is True:
                return quiet("heat_topic_not_ready")
            self.provider_success(settings)
            self._group_failures.pop(scope,None)
            def candidate_current(*, check_heat_budget=False):
                plan=self._group_target_context.get()
                if plan is not None:
                    return time.monotonic()-decision_started <= (15 if heated else 45) and self._group_target_current(plan)
                if named:
                    return named_current()
                if not heated:
                    return (time.monotonic()-decision_started<=45
                        and self.groups.is_latest_user_event(scope,event.user_id,event.event_id)
                        and self.groups.recent_user_event(scope,event.user_id,event.event_id))
                # A hot room may keep speaking while a short reply is generated.
                # Only a fresh, unreplaced anchor survives; privacy invalidation
                # and the generation guard are checked independently below.
                if (time.monotonic()-decision_started>15
                    or not self.groups.is_latest_user_event(scope,event.user_id,event.event_id)
                    or not self.groups.recent_user_event(scope,event.user_id,event.event_id)):
                    return False
                return not check_heat_budget or self.groups.heat(scope,settings)["eligible"]

            if (not self._group_allowed_now(event,demo=demo) or context_generation!=self.generation(scope)
                or not candidate_current(check_heat_budget=True)):
                # The obsolete request still incurred a provider call. Keep
                # its rate-limit timestamp so a busy group cannot repeatedly
                # spend another call while new messages invalidate answers.
                # Preserve the old cutoff: these messages remain unprocessed
                # context for the next eligible incoming event, with no timer.
                if self._group_allowed_now(event,demo=demo) and context_generation==self.generation(scope):
                    self._group_decisions[scope]=(last_cutoff,decision_started)
                return quiet("context_changed_during_generation")
            if answer.get('_local_observe'):
                return quiet('low_confidence' if answer.get('_local_observe_reason')=='below_participation_threshold' else 'conversation_observed')
            if not answer.get('_participation_plan'):
                return quiet('conversation_plan_invalid')
            response=clean_reply(answer.get("text","") if isinstance(answer.get("text"),str) else "")
            if not response or len(response)>settings.group_chat_max_reply_chars:
                return quiet("invalid_group_reply")
            if (not requests_repeat(event.text) and any(SequenceMatcher(None,response,r["text"]).ratio()>.86 for r in context if r["role"]=="assistant")):
                return quiet("repetitive_reply")
            reservation=None if named else self.groups.reserve(scope,heat_limit=settings.group_heat_max_replies if heated else None,**budget)
            if not named and not reservation:
                return quiet("group_speaking_budget")
            # All output types share the same single speaking reservation.
            # The heat slot has now been reserved, so do not re-check whether
            # an additional quota slot remains when validating this same reply.
            valid=lambda: self._group_allowed_now(event,demo=demo) and candidate_current()
            result=await self._render_reply(event,scope,settings,answer,response,trace_data,
                trigger="group_observation",context_generation=context_generation,valid=valid)
            if result["mode"]=="none":
                return quiet(result.get("reason","context_changed_during_generation"))
            return self._finish_group(event,scope,reply_id,result,
                                      demo=demo,reservation=reservation,thread_kind="start")

    def _preview_context_allowed(self, scope, context):
        # An observed member can decline saved text while still allowing the
        # short-lived conversation. Do not archive a response derived from that
        # member's words via another member's triggering message.
        users={row["user_id"] for row in context or [] if isinstance(row,dict)
               and isinstance(row.get("user_id"),str) and row.get("role") != "assistant"}
        return all(self.memory.collection_source(scope,user)!="member_disabled" and not self.groups.opted_out(scope,user)
                   for user in users)

    def _group_prompt(self,event,scope,settings,context,*,trace=None,memories=None,addressed=False,heat=None,dialogue=None,peer_bot=False):
        policy=prompt(self.content_root,"group",settings)
        policy += "\n" + "\n".join(prompt(self.content_root,key,settings) for key in ("knowledge","member_memory","member_context_use","quoted_origin","expression","vision","output","style","dialogue","conversation_target"))
        dialogue=self._dialogue_context(event,scope,settings) if dialogue is None else dialogue
        from .member_context import select as member_context
        memories = member_context(self.memory,scope,event.user_id,event.text,recent=list(context)+list(dialogue),related_users=self._related_members(event,scope)) if memories is None else memories
        knowledge = lore.for_prompt(self.content_root,event.text)
        persona = (self.content_root/"content/persona.md").read_text()
        expressions = assets.catalog(self.content_root,event.text)
        data={"confirmed_direct_dialogue":dialogue,"current_message":self._current_message(event),
              "available_expressions":expressions,"recent_group_messages":context,"latest_speaker_id":event.user_id,
              "latest_speaker_familiarity":self.groups.familiarity(scope,event.user_id),
              "latest_speaker_own_memories":memories,
              "lore":knowledge}
        if peer_bot:
            data["current_message"]["is_bot"] = True
        if heat is not None:
            data["group_activity"]=heat
            policy += "\n"+prompt(self.content_root,"heat",settings)
        names=getattr(self,"address_names_loader",lambda:[])()
        if not peer_bot and (addressed or is_addressed(event.text,names)):
            data["addressed_candidate"]={"user_id":event.user_id,"message_id":event.event_id,"text":event.text}
            policy += prompt(self.content_root,"addressed",settings)
        if trace is not None:
            trace.update(trace_references(memories=memories,knowledge=knowledge,persona=persona,
                                          quote_origin=data['current_message'].get('quote_origin'),
                                          group_context=context,dialogue=dialogue,input_characters=len(event.text),source_event_id=event.event_id,expressions=expressions))
            trace["_text_preview_allowed"] = self._preview_context_allowed(scope,context)
            if heat is not None:trace["group_activity"]=heat
        return [{"role":"system","content":with_persona(self.content_root,persona,policy,settings)},
                {"role":"user","content":prompt(self.content_root,"group_header",settings)+json.dumps(data,ensure_ascii=False)}]

    async def _reply(self, event: Incoming, scope: str, settings: Settings, *, demo: bool, group_context=None, group_thread=None) -> dict:
        text = event.text.strip()
        owner = settings.is_owner(event.kind, event.scope_id, event.user_id, event.transport)
        if len(text) > settings.max_input_chars:
            return {"mode": "text", "text": "这条有点长，请分段发给我。"}
        if GroupConversation.contains_credentials(text):
            return {"mode": "text", "text": "这条可能包含密钥，我没有保存或转发它。请在本机管理台的模型与语音页面配置。"}
        if text.split(maxsplit=1)[:1] == ["/订阅"]:
            if demo:
                return {"mode": "text", "text": "对话测试不会修改真实订阅。请在 QQ 中使用 /订阅 帮助，或在管理页配置。"}
            handler = getattr(self, "subscription_handler", None)
            return {"mode": "text", "text": await handler(event,owner) if handler else "订阅指令尚未连接，请在管理页配置。"}
        cmd = self.command(event, scope, text, owner)
        if cmd is not None:
            return {"mode": "text", "text": cmd}
        if text.startswith("/表情 "):
            return self.asset(text[4:].strip())
        mode_override = "voice" if text.startswith("/语音 ") else "text" if text.startswith("/文字 ") else None
        if mode_override:
            text = text[4:].strip()
        if not text and not event.mention_only:
            return {"mode": "none", "text": ""}
        if not self.is_active():
            return {"mode":"none","text":"","reason":"profile_changed"}
        # Observation is independent of a successful cloud response. Consent,
        # source deduplication and retention are enforced by the memory store.
        if text:self.memory.add_history(scope,event.user_id,"user",text,event.event_id)
        if settings.provider_mode=="cloud" and time.monotonic()<self._cloud_retry_after:
            return {"mode":"none","text":"","reason":"cloud_backoff"}
        context_generation=self.generation(scope)
        group_thread=self._dialogue_context(event,scope,settings) if group_thread is None else group_thread
        from .member_context import select as member_context
        memories = member_context(self.memory,scope,event.user_id,text,recent=list(group_context or [])+list(group_thread),related_users=self._related_members(event,scope))
        knowledge = lore.for_prompt(self.content_root, text)
        # Durable history is for memory management, not implicit conversation
        # replay. Every current conversation uses only recent confirmed turns.
        history = []
        persona = (self.content_root / "content/persona.md").read_text()
        policy = prompt(self.content_root,"direct",settings)
        if event.kind=="group" and is_addressed(event.text,getattr(self,"address_names_loader",lambda:[])()):
            policy += prompt(self.content_root,"addressed",settings)
        if event.mention_only:policy += '\n'+prompt(self.content_root,'mention_only',settings)
        policy += "\n" + "\n".join(prompt(self.content_root,key,settings) for key in ("knowledge","member_memory","member_context_use","quoted_origin","expression","vision","output","style","dialogue","conversation_target"))
        expressions = [] if mode_override else assets.catalog(self.content_root,text)
        context = {"current_message":self._current_message(event),
                   "available_expressions":expressions,"trusted_identity": {"kind":event.kind, "user_id":event.user_id, "is_owner":owner},
                   "retrieved_memory": memories, "lore": knowledge}
        if group_context is not None:
            context.update(recent_group_messages=group_context,
                           member_familiarity=self.groups.familiarity(scope,event.user_id))
        if group_thread:
            context["confirmed_direct_dialogue"]=group_thread
        messages = [{"role":"system", "content":with_persona(self.content_root,persona,policy,settings)},
                    {"role":"user", "content":prompt(self.content_root,"direct_header",settings) + json.dumps(context, ensure_ascii=False)}]
        messages.extend({"role": r["role"], "content": r["text"]} for r in history
                        if r["role"] in {"user","assistant"} and r["event_id"] != event.event_id)
        messages.append({"role":"user", "content":text})
        trace_data = trace_references(memories=memories,knowledge=knowledge,history=history,persona=persona,
                                      quote_origin=context['current_message'].get('quote_origin'),
                                      group_context=group_context,dialogue=group_thread,input_characters=len(text),
                                      source_event_id=event.event_id,expressions=expressions)
        trace_data["_text_preview_allowed"] = self._preview_context_allowed(scope,group_context)
        request_kind='mention' if event.mention_only else 'message'
        request_text=text if event.mention_only else event.text
        trace_id = None if event.transport=="onebot" and event.kind=="group" and settings.group_chat_enabled else self._trace_start(settings,"reply","direct",scope,event.user_id,trace_data,request_text=request_text,request_kind=request_kind)
        try:
            provider = self.providers(settings)
        except Exception as exc:
            self.trajectories.finish(trace_id,"failed",error=exc)
            raise
        answer = await self._complete_traced(provider,messages,settings,kind="reply",trigger="direct",
                                            scope=scope,user=event.user_id,refs=trace_data,trace_id=trace_id,context_generation=context_generation,
                                            image_event_id=event.event_id,history_events={r["id"]:r["event_id"] for r in history},
                                            request_text=request_text,request_kind=request_kind)
        trace_id = trace_data.pop("_trace_id",trace_id)
        if not self.is_active():
            return {"mode":"none","text":"","reason":"profile_changed"}
        if context_generation!=self.generation(scope):
            return {"mode":"none","text":"","reason":"privacy_context_changed"}
        if not isinstance(answer, dict):
            raise ProviderError("invalid_reply_object")
        if answer.get("_local_observe"):
            return {"mode":"none","text":"","reason":"conversation_observed"}
        response = clean_reply(answer.get("text", "") if isinstance(answer.get("text"), str) else "")
        if not response:
            raise ProviderError("empty_reply")
        self.provider_success(settings)
        result = await self._render_reply(event,scope,settings,answer,response,trace_data,
            trigger="direct",context_generation=context_generation,provider=provider,
            mode_override=mode_override,related_id=trace_id)
        if result["mode"]=="none":
            return result
        # An image selection must not pretend its fallback text was spoken.
        # Actual assistant history is committed by acknowledge(), including only
        # the confirmed prefix of a partially delivered segmented reply.
        # Automatic member-note extraction belongs to the background worker;
        # never delay an already generated reply with another cloud request.
        if not self.is_active():
            self.discard_generated_media(result)
            return {"mode":"none","text":"","reason":"profile_changed"}
        if context_generation!=self.generation(scope):
            self.discard_generated_media(result)
            return {"mode":"none","text":"","reason":"privacy_context_changed"}
        if demo:
            result.update(memories=[r for r in memories if r["id"] in {v["id"] for v in trace_data["memories"]}],
                          lore=[r for r in knowledge if r["chunk_id"] in {v["chunk_id"] for v in trace_data["knowledge"]}])
        result["_generated"]=True
        return result

    async def _render_reply(self,event,scope,settings,answer,response,trace_data,*,trigger,
                            context_generation,provider=None,mode_override=None,related_id=None,valid=None,
                            voice_only=False):
        """One output boundary for direct replies, named turns and follow-ups.

        This only prepares media; the bridge remains the sole sender. Any
        generation-time revocation cancels both audio and fallback text.
        """
        def current():
            return (self.is_active() and context_generation==self.generation(scope)
                    and (valid is None or valid())
                    and (not self._group_target_context.get() or self._group_target_current(self._group_target_context.get())))

        def quiet():
            return {"mode":"none","text":"","reason":"privacy_context_changed" if self.is_active() else "bot_stopped"}

        def voice_unavailable(reason):
            if voice_only:
                return {"mode":"none","text":"","reason":reason}
            return {"mode":"text","text":response}

        if not current():
            return quiet()
        if answer.get("_web_evidence"):
            response=web_search.cite(dict(answer,text=response),answer["_web_evidence"],show=settings.web_search_show_sources or web_search.wants_sources(event.text))["text"]
        mode="voice" if voice_only else mode_override or answer.get("mode","text")
        result=({"mode":"text","text":response} if voice_only or mode_override else
                self._expression_reply(answer,response,trace_data.get("assets",[])))
        if result["mode"]=="image" or mode!="voice" or (not voice_only and re.search(r"https?://|```",response)):
            return result

        from .group_voice import voice_settings_error
        is_group=event.kind=='group'
        if error := voice_settings_error(settings,response,group=is_group):
            return voice_unavailable(error)
        voice_trace=None;voice_reservation=None;voice_called=False
        try:
            # Recheck effective Profile voice settings before beginning a new
            # billable operation. GUI edits also invalidate the generation.
            latest=self.settings(scope)
            if not current():
                return quiet()
            if error := voice_settings_error(latest,response,group=is_group):
                return voice_unavailable(error)
            if is_group:
                voice_reservation=self.group_voice.reserve(scope,latest)
                if voice_reservation is None:
                    return voice_unavailable("group_voice_limit")
            provider=provider if provider is not None else self.providers(latest)
            voice_trace=self._trace_start(latest,"tts",trigger,scope,event.user_id,
                trace_references(input_characters=len(response),source_event_id=event.event_id),
                related_id=related_id if related_id is not None else trace_data.get("_trace_id"),
                model_override=getattr(provider,"fish_model",None),request_text=response,request_kind="speech")
            if settings.provider_mode=="cloud" and not self.ledger.consume(settings.daily_cloud_requests):
                raise ProviderError("voice_budget_exhausted")
            if not current():
                raise ProviderError("privacy_context_changed")
            self.trajectories.called(voice_trace)
            voice_called=True
            path=await provider.synthesize(response)
            result={"mode":"voice","text":response,"media_path":str(path)}
            latest=self.settings(scope)
            if (not current() or not latest.voice_enabled or not latest.voice_authorized
                or latest.fish_reference_id!=settings.fish_reference_id or latest.fish_model!=settings.fish_model):
                self.discard_generated_media(result)
                self.trajectories.finish(voice_trace,"interrupted",error="privacy_context_changed", cancellation_reason=self.context_change_reason(scope,context_generation))
                return quiet()
            self.trajectories.record_response(voice_trace,response,kind="speech")
            self.trajectories.finish(voice_trace,result={"mode":"voice"})
            self.diagnostics.record("tts","ok")
            return result
        except asyncio.CancelledError:
            if voice_trace is not None:
                self.trajectories.finish(voice_trace,"interrupted")
            self.discard_generated_media(result)
            raise
        except ProviderError as exc:
            invalid=not current() or str(exc) in {"bot_stopped","privacy_context_changed"}
            if voice_trace is not None:
                self.trajectories.finish(voice_trace,"interrupted" if invalid else "failed",error=exc, cancellation_reason=self.context_change_reason(scope,context_generation) if invalid else None)
            if invalid:
                self.discard_generated_media(result)
                return quiet()
            code=safe_code(exc)
            self.diagnostics.record("tts",code)
            log.warning("voice_generation_failed")
            # Synthesis failed before sending anything: keep one actual text
            # reply for ordinary replies, or report failure for a voice-only send.
            return voice_unavailable("voice_generation_failed" if code=="provider_unknown_error" else code)
        except Exception as exc:
            if voice_trace is not None:
                self.trajectories.finish(voice_trace,"failed",error=exc)
            self.discard_generated_media(result)
            raise
        finally:
            if voice_reservation is not None and not voice_called:
                self.group_voice.release(voice_reservation)

    async def summarize_due(self):
        """One fair, bounded background extraction in an enabled live scope."""
        settings=self.settings()
        if not self.is_active():
            return {"status":"profile_changed","saved":0}
        if self._capacity.locked():
            return {"status":"reply_busy","saved":0}
        if not ((settings.transport=="onebot" and settings.onebot_enabled) or
                (settings.transport=="official" and settings.qq_enabled)):
            return {"status":"transport_disabled","saved":0}
        if settings.transport=="onebot":
            allowed_scopes=([f"onebot:{settings.onebot_self_id}:group:{target}" for target in settings.onebot_allowed_groups]
                           +[f"onebot:{settings.onebot_self_id}:dm:{target}" for target in settings.onebot_allowed_dm_users])
        else:
            allowed_scopes=([f"qq:{settings.app_id}:group:{target}" for target in settings.allowed_groups]
                           +[f"qq:{settings.app_id}:dm:{target}" for target in settings.allowed_dm_users])
        for member in self.memory.due_summaries(threshold=settings.auto_summary_every,limit=20,allowed_scopes=allowed_scopes,
                max_wait_seconds=settings.auto_summary_max_wait_minutes*60):
            if self.groups.opted_out(member['scope'],member['user']):
                continue
            parts=member["scope"].split(":")
            if len(parts)!=4:
                continue
            platform,self_id,kind,target=parts
            allowed=(platform=="onebot" and settings.transport=="onebot" and self_id==settings.onebot_self_id and
                     ((kind=="group" and target in settings.onebot_allowed_groups) or
                      (kind=="dm" and target==member["user"] and target in settings.onebot_allowed_dm_users)))
            allowed=allowed or (platform=="qq" and settings.transport=="official" and self_id==settings.app_id and
                     ((kind=="group" and target in settings.allowed_groups) or
                      (kind=="dm" and target==member["user"] and target in settings.allowed_dm_users)))
            if allowed:
                async with self._capacity:
                    # Capacity can be occupied by a slow reply. Recheck the
                    # live whitelist after waiting, before sending saved text.
                    latest=self.settings()
                    still_allowed=(platform=="onebot" and latest.transport=="onebot" and latest.onebot_enabled
                        and self_id==latest.onebot_self_id and
                        ((kind=="group" and target in latest.onebot_allowed_groups) or
                         (kind=="dm" and target==member["user"] and target in latest.onebot_allowed_dm_users)))
                    still_allowed=still_allowed or (platform=="qq" and latest.transport=="official" and latest.qq_enabled
                        and self_id==latest.app_id and
                        ((kind=="group" and target in latest.allowed_groups) or
                         (kind=="dm" and target==member["user"] and target in latest.allowed_dm_users)))
                    if not self.is_active() or not still_allowed:
                        return {"status":"policy_changed","saved":0}
                    return await self.summarize(member["scope"],member["user"],None,latest,trigger="background")
        return {"status":"idle","saved":0}

    async def summarize(self, scope: str, user: str, provider, settings: Settings, *, force=False, trigger=None, revisit=False):
        # Snapshot this profile/group's cadence for both event and timer paths.
        settings=Settings.model_validate(settings.model_dump() | self.memory.summary_policy(scope))
        state = read_control(self.root)
        if not state["enabled"]:
            return {"status": "bot_stopped", "saved": 0}
        lease = self._activity_epoch.set(self._activity_epoch.get() or state["epoch"])
        try:
            return await self._summarize(scope, user, provider, settings, force=force, trigger=trigger,revisit=revisit)
        finally:
            self._activity_epoch.reset(lease)

    async def _summarize(self, scope: str, user: str, provider, settings: Settings, *, force=False, trigger=None,revisit=False):
        if not self.is_active():
            return {"status":"profile_changed","saved":0}
        context_generation = self.generation(scope)
        claim=self.memory.begin_summary(scope,user,threshold=settings.auto_summary_every,force=force,
                                        max_wait_seconds=settings.auto_summary_max_wait_minutes*60,revisit=revisit)
        if claim["status"] != "ready":
            return claim
        batch=claim["batch"]
        from .member_notes import background as member_background,summary_dialogue
        pending=summary_dialogue(self.memory,scope,user,batch["history"])
        batch['history']=pending
        previous=member_background(self.memory,scope,user)
        batch["previous_background"]=previous
        trace_data = trace_references(history=pending,memories=[previous] if previous else [])
        trigger = "manual_revisit" if revisit else "manual" if force else trigger or "automatic"
        trace_id = self._trace_start(settings,"summary",trigger,scope,user,trace_data,
            request_text=pending[-1]["text"] if pending else None,request_kind="summary_source")
        if settings.provider_mode == "cloud" and time.monotonic() < self._cloud_retry_after:
            self.trajectories.finish(trace_id,"cloud_backoff")
            return self.memory.fail_summary(scope,user,batch,"cloud_backoff",max(60,round(self._cloud_retry_after-time.monotonic())))
        try:
            # Most observed messages do not yet form a due batch. Resolve
            # credentials only once a real extraction is ready and budgeted.
            evidence=[dict(r,observed_at=datetime.fromtimestamp(r['created_at'],timezone.utc).isoformat()) for r in pending]
            if previous:
                evidence[0]['previous_member_background']={k:previous[k] for k in ('id','text','source','version')}
            data = await self._complete_traced(provider,[
                {"role":"system", "content":prompt(self.content_root,"summary",settings)+'\n'+prompt(self.content_root,"member_summary",settings)},
                {"role":"user", "content":json.dumps(evidence, ensure_ascii=False)}],settings,
                kind="summary",trigger=trigger,scope=scope,user=user,refs=trace_data,trace_id=trace_id,context_generation=context_generation,
                request_text=pending[-1]["text"] if pending else None,request_kind="summary_source")
            trace_id = trace_data.pop("_trace_id",trace_id)
            retained = {r["id"] for r in trace_data["history"]}
            batch["history"] = [r for r in pending if r["id"] in retained]
            if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
                # A missing extraction field is a malformed response, not proof
                # that this member said nothing worth remembering.
                raise ValueError("summary_invalid")
            if not self.is_active():
                self.trajectories.finish(trace_id,"profile_changed")
                return self.memory.fail_summary(scope,user,batch,"cloud_backoff",60)
            result=self.memory.finish_summary(scope,user,batch,data.get("memories",[]),
                background=data.get('background'),interactions=data.get('interactions'))
            if result["status"] in {"saved","no_facts"}:
                summary_preview=[{"text":item["text"],"source_ids":[n for n in item.get("source_ids",[])[:100] if type(n) is int]}
                    for item in (data.get("memories",[])[:6]+([data["background"]] if data.get("background") else [])+(data.get("interactions") or [])) if isinstance(item,dict) and isinstance(item.get("text"),str)
                    and isinstance(item.get("source_ids"),list)]
                self.trajectories.record_response(trace_id,json.dumps(summary_preview,ensure_ascii=False),kind="summary")
            self.trajectories.finish(trace_id,result["status"],result=result)
            if settings.provider_mode=="cloud":
                self.diagnostics.record("summary","ok")
            return result
        except (ProviderError, ValueError, PermissionError, TypeError) as exc:
            trace_id = trace_data.pop("_trace_id",trace_id)
            if isinstance(exc,ProviderError) and str(exc) in {"cloud_budget","context_retry_budget"}:
                self.trajectories.finish(trace_id,"cloud_budget",error=exc)
                return self.memory.fail_summary(scope,user,batch,"cloud_budget",900)
            if isinstance(exc, ProviderError) and str(exc) == "bot_stopped":
                self.trajectories.finish(trace_id,"interrupted")
                return self.memory.fail_summary(scope,user,batch,"cloud_backoff",60)
            self.trajectories.finish(trace_id,"failed",error=exc if isinstance(exc,ProviderError) else "summary_invalid")
            self.diagnostics.record("summary",safe_code(exc) if isinstance(exc,ProviderError) else "summary_invalid")
            log.warning("summary_skipped")
            return self.memory.fail_summary(scope,user,batch,"provider_unavailable" if isinstance(exc,ProviderError) else "summary_invalid",60)

    def command(self, event: Incoming, scope: str, text: str, owner: bool) -> str | None:
        user = event.user_id
        group_collection = event.kind == "group" and self.memory.group_collection_enabled(scope)
        memory_policy = ("本群已开启全员默认记忆，收到的合格文字按每位成员分别保存和整理；个人关闭设置优先。"
                         if group_collection else "本范围未开启全群默认记忆，可用 /记忆 开启 设置本人的长期记录。")
        if text in {"/帮助", "/隐私"}:
            return ("单条消息以 （（ 或 (( 开头（前面可留空格），本条文字和附件会被忽略：不回复、不送模型、不进入上下文、历史、记忆或调用轨迹；被引用时也不读取其内容。下一条正常消息照常处理。此标记不会撤回 QQ 消息，也不会隐藏其他群友手动转述的内容。\n"
                    "白名单群开启群聊参与时，最近图文会作为短期上下文发往已配置的云端模型判断是否接话；图片理解可由管理员关闭，图片只在内存短期缓存，GIF仅处理首帧。群短期上下文不整段落盘；本机调用轨迹另保存触发消息末尾最多120字和回复最多2000字供管理员排查，随轨迹保留最多30天，删除/纠错来源会清理相关预览。可用 /旁听 关闭 退出并停止保存本人新的轨迹文本。\n"
                    +memory_policy+"原文最多保留30天、每人200条；整理时会发给已配置的云端模型。记忆限当前角色、群和本人，推断可纠正；群内回复可能提及本群保存的偏好。用 /记忆 关闭 停止并清除自动记录，或 /忘记我 清除个人记录。\n"
                    "/身份 · /旁听 开启 · /旁听 关闭 · /记忆 开启 · /记忆 关闭 · /记忆 · /记住 内容 · /纠正 编号 内容 · /忘记 编号 · /忘记我 · /语音 内容 · /文字 内容 · /表情 名称 · /订阅 帮助\n"
                    "主人可用 /群记住、/事件、/关系 保存公共条目。撤回QQ消息不会自动删除数据库；请使用删除指令。")
        if text == "/身份":
            return f"群/私聊范围：{scope}\n成员ID：{user}\n管理身份：{'主人' if owner else '普通成员'}\n身份来自QQ事件，昵称不参与授权。"
        if text in {"/记忆 开启", "/记忆 关闭"}:
            enabled = text.endswith("开启")
            self.memory.set_consent(scope,user,enabled)
            if not enabled:
                self.trajectories.erase_image_sources(scope,user)
                self.clear_group_context(scope)
            return "已开启当前范围的聊天保存与自动摘要；可用 /忘记我 删除。" if enabled else "已停止自动记忆，并清除聊天原文与推断摘要；显式保存的条目仍可用 /忘记我 删除。"
        if text in {"/旁听", "/旁听 开启", "/旁听 关闭"}:
            if event.kind != "group" or event.transport != "onebot":
                return "旁听设置适用于普通 QQ 的当前群；私聊不会加入群上下文。"
            if text == "/旁听":
                return "你已退出当前群的旁听。用 /旁听 开启 可恢复。" if self.groups.opted_out(scope,user) else "你的群消息可加入短期上下文；用 /旁听 关闭 可退出。"+memory_policy
            excluded=text.endswith("关闭")
            self.groups.set_optout(scope,user,excluded)
            if excluded:
                self.trajectories.erase_previews(scope=scope)
                self.clear_group_context(scope)
            return "已退出当前群旁听，清除你的昵称计数与短期上下文；你主动 @我或发指令仍可得到回复。长期记录可用 /忘记我 清除。" if excluded else "已恢复当前群旁听。"+memory_policy
        if text == "/忘记我":
            self.forget_person(scope,user)
            return "已删除当前范围内你的个人记忆、聊天和熟悉度，关闭自动记忆并退出旁听；旧备份需由本机管理员另行处理。"
        if text == "/记忆":
            items = self.memory.list_memories(scope,user)
            return "\n".join(f"#{m['id']} [{m['kind']}/{m['source']}] {m['text']}" for m in items[:20]) or "当前没有可查看的记忆。"
        for prefix, kind in (("/记住 ","personal"),("/群记住 ","group"),("/事件 ","event"),("/关系 ","relation")):
            if text.startswith(prefix):
                i = self.memory.add_memory(scope,user,text[len(prefix):].strip(),kind=kind,is_owner=owner)
                return f"已保存 #{i}；可用 /纠正 {i} 内容 或 /忘记 {i} 管理。"
        if text.startswith("/忘记 "):
            self.memory.delete_memory(int(text.split(maxsplit=1)[1]),scope,user,is_owner=owner)
            self.clear_group_context(scope)
            return "该条记忆已删除。"
        if text.startswith("/纠正 "):
            _, number, value = text.split(maxsplit=2)
            self.memory.update_memory(int(number),scope,user,value,is_owner=owner)
            self.clear_group_context(scope)
            return "该条记忆已更新。"
        return None

    def asset(self, name: str) -> dict:
        try:
            return assets.prepare_asset(self.content_root,self.root/"data/media",name)
        except assets.AssetError:
            return {"mode":"text", "text":"还没有配置这个自定义表情，或它已被停用。"}

    def _expression_reply(self, answer, fallback: str, candidates) -> dict:
        result = {"mode":"text","text":fallback}
        if answer.get("mode") != "image" or not isinstance(answer.get("asset_id"),str):
            return result
        candidate = next((row for row in candidates if row.get("id")==answer["asset_id"]),None)
        if candidate is None:
            return result
        try:
            selected = assets.prepare_asset(self.content_root,self.root/"data/media",candidate["id"],automatic=True)
        except assets.AssetError:
            return result
        if selected["asset_reference"]["revision"] != candidate["revision"]:
            self.discard_generated_media(selected)
            return result
        selected["_context_text"] = "[发送自定义表情："+candidate["name"]+"]"
        return selected
