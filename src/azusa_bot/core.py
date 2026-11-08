from __future__ import annotations

from .prompts import DEFAULTS as PROMPT_DEFAULTS, prompt, overrides as prompt_overrides, revision as prompt_revision

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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ConfigDict, model_validator

from . import lore, assets, web_search, group_topic, participation_vote, conversation_config, segmented_delivery
from .memory import MemoryStore
from .group_chat import GroupConversation
from .dialogue import RecentDialogue, refers_back, requests_repeat
from .providers import CloudProviders, MockProviders, ProviderError
from .provider_usage import CompletionResult
from .diagnostics import Diagnostics, safe_code
from .secrets import load_credentials, resolve_credentials
from .credential_store import CredentialStoreError
from .settings import QQ_ID_PATTERN, Settings, read_settings
from .trajectories import references as trace_references, TrajectorySourceErased
from .bot_control import activity_allowed, read_control
from .context_budget import fit_context, estimate_tokens
from .vision import VisionCache
from .vision_prompt import VISION_POLICY, retained_events, attach_images, visual_sources
from .group_addressing import is_addressed
from .accounts import AccountError, managed_qq_ids

log = logging.getLogger("azusa")
ID = r"^[A-Za-z0-9_-]+$"
GROUP_COMMANDS = {"/帮助", "/隐私", "/身份", "/记忆", "/记住", "/群记住", "/事件", "/关系", "/忘记", "/纠正", "/忘记我", "/语音", "/文字", "/表情", "/旁听", "/订阅"}


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
    unprompted: bool = False
    display_name: str = Field(default="", max_length=80)
    reply_to_id: str | None = Field(default=None, max_length=21, pattern=r"^-?[0-9]{1,20}$")
    images: list[Annotated[str,Field(max_length=8192)]] = Field(default_factory=list, max_length=3)
    activity_only: bool = False

    @model_validator(mode="after")
    def validate_onebot_ids(self):
        if self.transport == "onebot" and not all(
            re.fullmatch(QQ_ID_PATTERN, value) for value in (self.app_id, self.scope_id, self.user_id)
        ):
            raise ValueError("OneBot requires numeric self, group, and user IDs")
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

    def reserve(self, event: Incoming, scope: str, profile_id: str = "default", profile_epoch: str = "") -> str | None:
        key = hashlib.sha256((scope + "|" + event.event_id).encode()).hexdigest()
        reply_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute("DELETE FROM deliveries WHERE at < ?", (time.time() - 30*86400,))
            db.execute("DELETE FROM delivery_segments WHERE reply_id NOT IN (SELECT reply_id FROM deliveries)")
            cur = db.execute("INSERT OR IGNORE INTO deliveries (event_key,reply_id,scope,user_id,status,mode,at,profile_id,profile_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, reply_id, scope, event.user_id, "processing", "none", time.time(),profile_id,profile_epoch))
            return reply_id if cur.rowcount else None

    def mark(self, reply_id: str, status: str, mode: str | None = None):
        with self.db() as db:
            if mode:
                db.execute("UPDATE deliveries SET status=?,mode=? WHERE reply_id=?", (status, mode, reply_id))
            else:
                db.execute("UPDATE deliveries SET status=? WHERE reply_id=?", (status, reply_id))

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
        self.memory = MemoryStore(data / "memory.sqlite3")
        self.trajectories = self.memory.trajectories
        (root / "data").mkdir(parents=True,exist_ok=True,mode=0o700)
        self.ledger = Ledger(root / "data/state.sqlite3")
        if profile_id == "default":
            with self.ledger.db() as db:
                for row in db.execute("SELECT scope,user_id,through_id FROM summaries"):
                    self.memory.import_summary_checkpoint(row["scope"],row["user_id"],row["through_id"])
        self.diagnostics = Diagnostics(root / "data/state.sqlite3")
        self._cloud_failure_count = 0
        self._cloud_retry_after = 0.0
        self.search_override = None
        initial_settings=read_settings(root)
        self.groups = GroupConversation(data / "group_state.sqlite3",
            max_messages=initial_settings.group_chat_context_messages,
            ttl_seconds=initial_settings.group_chat_context_minutes*60)
        self._group_decisions: dict[str, tuple[float, float]] = {}
        self._group_pending: dict[str, dict] = {}
        self._group_failures: dict[str, tuple[int, float]] = {}
        self._group_generation = 0
        self._scope_generations = {}
        self._context_changes = {}
        self._group_threads: dict[tuple[str,str],dict] = {}
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

    def provider_failure(self, error, settings):
        if str(error) == "bot_stopped":
            return
        code = safe_code(error)
        self.diagnostics.record("llm",code)
        if code == "privacy_context_changed":
            log.info("reply_cancelled code=%s", code)
            return
        log.warning("provider_failure code=%s",code)
        if code in {"context_budget_exceeded","cloud_budget","context_retry_budget","privacy_context_changed"}:
            return
        if settings.provider_mode == "cloud":
            self._cloud_failure_count = min(5,self._cloud_failure_count+1)
            self._cloud_retry_after = time.monotonic()+min(900,30*2**self._cloud_failure_count)

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
        if kind in {"group_decision","followup","group_idle"}:
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
        if kind=='group_idle':
            from .role_activity import augment
            messages=augment(self,messages,settings,kwargs)
        if kind not in {"reply","group_decision","followup"}:
            return await self._complete_traced_once(provider,messages,settings,**kwargs)
        messages=await group_topic.prepare(self,provider,messages,settings,kwargs)
        from . import role_activity, knowledge_lookup
        messages=role_activity.augment(self,messages,settings,kwargs)
        messages=copy.deepcopy(messages)
        messages[0]["content"] += "\n"+prompt(self.content_root,"search" if settings.web_search_enabled else "search_offline",settings)
        messages[0]['content']+='\n'+prompt(self.content_root,'knowledge_lookup',settings)
        messages[0]['content']+='\n'+prompt(self.content_root,'history_lookup',settings)
        from .execution import run_dialogue
        return await run_dialogue(self,provider,messages,settings,kwargs)

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
                               execution=None):
        generation = self.generation(scope) if context_generation is None else context_generation
        config = conversation_config.read(self.content_root)
        example_messages = conversation_config.examples(config) if kind in {"reply", "group_decision", "followup", "group_idle"} else []
        example_tokens = estimate_tokens(example_messages) - 256
        if example_messages:
            messages = copy.deepcopy(messages)
            example_policy = prompt(self.content_root, "examples", settings)
            if example_policy not in messages[0]["content"]:
                messages[0]["content"] += "\n" + example_policy
        refs["begin_dialogs"] = {"pairs": len(example_messages) // 2, "revision": conversation_config.example_revision(config),
                                "estimated_tokens": example_tokens}
        if participation_vote.enabled(settings,kind):
            messages=copy.deepcopy(messages)
            vote_prompt=prompt(self.content_root,"participation_vote",settings)
            if vote_prompt not in messages[0]["content"]:
                messages[0]["content"] += "\n"+vote_prompt
        refs["prompt_prefixes"]={key:prompt(self.content_root,key,settings) for key in ("direct_header","group_header","followup_header")}
        refs["prompt_revision"]=prompt_revision(prompt_overrides(self.content_root))
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
        prepared = fit_context(messages, refs, kind=kind, window_tokens=settings.context_window_tokens,
                               output_tokens=700 if kind == "summary" else 1200,image_tokens=image_tokens,example_tokens=example_tokens)
        number = trace_id
        parent = related_id
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
                current_refs["delivery_event_key"] = hashlib.sha256((scope+"|"+str(image_event_id)).encode()).hexdigest()
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
                    raise ProviderError("context_retry_budget" if attempt == 2 else "cloud_budget")
                if execution is not None:
                    execution.consume()
                provider = provider if provider is not None else self.providers(settings)
                self.trajectories.called(number)
                request_messages = attach_images(prepared["messages"],selected,current_refs["images"],current_event=image_event_id,labels={key:prompt(self.content_root,key,settings) for key in ("earlier_image","image_label")})
                # Examples go before actual context only at the transport boundary.
                # Budgeting, image association and retrieval keep their typed layout.
                if example_messages:
                    request_messages = [request_messages[0], *copy.deepcopy(example_messages), *request_messages[1:]]
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
                answer=participation_vote.apply(answer,settings,kind)
                from .knowledge_lookup import query as knowledge_query
                from .history_lookup import query as history_query
                researching=(allow_history and history_query(answer,kind,settings) is not None) or (allow_search and self._wants_search(answer,kind,settings)) or (allow_knowledge and knowledge_query(answer,kind,settings) is not None)
                if kind == "reply" and not researching and not clean_reply(answer.get("text", "") if isinstance(answer.get("text"), str) else ""):
                    raise ProviderError("empty_reply")
                if kind not in {"summary","group_topic"}:
                    raw_response = answer.get("text", "") if isinstance(answer.get("text"),str) else ""
                    silent = kind in {"group_decision","followup","group_idle"} and (
                        answer.get("participate") is False or (kind == "followup" and answer.get("continuation") is False))
                    self.trajectories.record_response(number,"" if (silent and not answer.get("_participation_vote")) or researching else clean_reply(raw_response),
                        source_text=None if (silent and not answer.get("_participation_vote")) or researching else raw_response)
                    self.trajectories.finish(number,"research_requested" if researching else "success", result=dict(answer,
                        output_characters=len(answer.get("text", "")) if isinstance(answer.get("text"), str) else 0))
                if kind=="group_topic":
                    topic_prefix=refs["prompt_prefixes"]["group_header"]
                    refs["_topic_rows"]=json.loads(prepared["messages"][1]["content"][len(topic_prefix):])["recent_group_messages"]
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
                status = ("interrupted" if code in {"bot_stopped","privacy_context_changed"} else
                          "cloud_budget" if code in {"cloud_budget","context_retry_budget"} else "failed")
                self.trajectories.finish(number,status,error=None if code == "bot_stopped" else exc,
                    cancellation_reason=self.context_change_reason(scope,generation) if code=="privacy_context_changed" else None)
                if isinstance(exc,ProviderError) and code == "context_length_exceeded" and attempt == 1 and kind!="group_topic":
                    # Never retry an identical prompt, a generic 400, an outage,
                    # or incomplete output. A second attempt gets its own trace.
                    smaller = fit_context(prepared["messages"],prepared["refs"],kind=kind,
                        window_tokens=max(4096,settings.context_window_tokens//2),
                        output_tokens=700 if kind == "summary" else 1200,image_tokens=image_tokens,example_tokens=example_tokens)
                    if (not smaller["blocked"] and
                            smaller["metrics"]["estimated_after"] < prepared["metrics"]["estimated_after"]):
                        prepared, parent, number = smaller, number, None
                        continue
                raise
        raise ProviderError("context_length_exceeded")

    async def handle(self, event: Incoming, *, demo: bool = False) -> dict:
        original_text = event.text
        if event.images and not event.text.lstrip().startswith("/"):
            # Explicit transport annotation, never an invented picture caption.
            event = event.model_copy(update={"text":event.text.rstrip()+f"\n[用户发送{len(event.images)}张图片；图像内容仅在视觉附件中]"})
        state = read_control(self.root)
        if not state["enabled"]:
            return {"mode": "none", "text": "", "reason": "bot_stopped"}
        lease = self._activity_epoch.set(state["epoch"])
        audit_lease = self._request_audit_text.set(original_text)
        try:
            return await self._handle(event, demo=demo)
        finally:
            self._activity_epoch.reset(lease)
            self._request_audit_text.reset(audit_lease)
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
            if event.transport == "onebot":
                try:
                    if event.user_id in managed_qq_ids(self.root):
                        return {"mode": "none", "text": "", "reason": "managed_bot_sender"}
                except AccountError:
                    return {"mode": "none", "text": "", "reason": "account_registry_unavailable"}
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
        if not demo:
            self.idle.observe(event, scope)
        if event.activity_only:
            self.ledger.mark(reply_id, "activity_observed", "none")
            return {"reply_id":reply_id,"mode":"none","text":"","reason":"activity_observed"}
        group_mode = event.transport == "onebot" and event.kind == "group" and settings.group_chat_enabled
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
                return await self._handle_group(event,scope,settings,reply_id,demo=demo)
            except asyncio.CancelledError:
                key=(scope,event.user_id)
                if self._named_candidates.get(key,{}).get("event_id")==event.event_id:
                    self._named_candidates.pop(key,None)
                raise
            except Exception:
                self.diagnostics.record("core","internal_failure")
                log.error("group_processing_failed reply=%s",reply_id)
                return self._finish_group(event,scope,reply_id,
                    {"mode":"none","text":"","reason":"group_internal_failure"},demo=demo)
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
                result = {"mode": "none", "text": "", "reason":str(exc) if str(exc) in {"bot_stopped","cloud_budget","context_budget_exceeded","context_retry_budget","privacy_context_changed"} else "cloud_failure"}
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
            segmented_delivery.plan(self, event, result, demo=demo)
            return result

    def clear_group_context(self, scope: str, *, reason="context_cleared"):
        self._context_changes[scope] = (time.monotonic(), reason)
        self.idle.invalidate(scope)
        self.dialogue.clear(scope)
        self._scope_generations[scope]=self._scope_generations.get(scope,0)+1
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
        self._group_generation += 1
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

    def _group_snapshot(self, scope: str, settings: Settings):
        return self.groups.snapshot(scope,limit=settings.group_chat_context_messages,
                                    ttl_seconds=settings.group_chat_context_minutes*60)

    @staticmethod
    def _group_budget(settings: Settings):
        return dict(cooldown_seconds=settings.group_chat_cooldown_seconds,
                    hour_limit=settings.group_chat_hourly_limit,day_limit=settings.group_chat_daily_limit)

    def _group_allowed_now(self, event: Incoming, *, demo: bool) -> bool:
        settings = self.settings(f"{event.transport}:{event.app_id}:group:{event.scope_id}")
        return self.is_active() and settings.group_chat_enabled and (demo or (
            settings.transport == "onebot" and settings.onebot_enabled and
            settings.onebot_self_id == event.app_id and event.scope_id in settings.onebot_allowed_groups))

    def _finish_group(self, event, scope, reply_id, result, *, demo=False, reservation=None, thread_kind=None, thread_id=None):
        key=(scope,event.user_id)
        if self._named_candidates.get(key,{}).get("event_id")==event.event_id:
            self._named_candidates.pop(key,None)
        if not self.is_active():
            self.discard_generated_media(result)
            result={"mode":"none","text":"","reason":"profile_changed"}
        result.update(reply_id=reply_id,scope=scope)
        if result.get("mode") == "none":
            self.ledger.mark(reply_id,result.get("reason","quiet"),"none")
            return result
        self.ledger.mark(reply_id,"demo" if demo else "prepared",result["mode"])
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
                thread_id=thread_id,user_text=event.text,event_id=event.event_id,user_at=user_at,mode=result["mode"])
            if demo:
                self.acknowledge(reply_id,"sent",str(-int(reply_id[:12],16)),demo=True)
        segmented_delivery.plan(self, event, result, demo=demo)
        return result

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
            user_text=event.text,event_id=event.event_id,dialogue_only=True)

    def acknowledge(self, reply_id: str, status: str, message_id: str | None = None, *, demo=False):
        if not demo:
            self.ledger.mark(reply_id,status)
        pending = self._group_pending.pop(reply_id,None)
        if not self.is_active():
            return
        if pending and pending.get("thread_kind")=="continue" and (status!="sent" or not message_id):
            key=(pending["scope"],pending["user_id"])
            if self._group_threads.get(key,{}).get("id")==pending.get("thread_id"):
                self._group_threads.pop(key,None)
        if not pending or status != "sent" or not message_id or time.monotonic()-pending["created_at"]>=240:
            return
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
                            display_name="自己",role="assistant")
        self.idle.bot_spoke(pending["scope"])
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

    @staticmethod
    def _thread_context(thread):
        now=time.monotonic()
        return [dict(m,age_seconds=max(0,int(now-m["at"]))) for m in thread["messages"]]

    @staticmethod
    def _current_message(event):
        return dict(kind=event.kind,message_id=event.event_id,user_id=event.user_id,text=event.text,
                    has_images=bool(event.images),image_count=len(event.images),reply_to_id=event.reply_to_id)

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
        if pending.get("user_at") is not None:
            thread["messages"].append({"role":"user","user_id":user,"message_id":pending["event_id"],
                                       "text":pending["user_text"],"at":pending["user_at"]})
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
                        and self.groups.is_latest_user_event(scope,event.user_id,event.event_id)
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
        own_memories = self.memory.retrieve(scope,event.user_id,event.text)
        dialogue=self._dialogue_context(event,scope,settings)
        messages=self._group_prompt(event,scope,settings,context,trace=trace_data,memories=own_memories,dialogue=dialogue)
        messages[0]["content"] += prompt(self.content_root,"followup",settings)
        messages.append({"role":"user","content":prompt(self.content_root,"followup_header",settings)+json.dumps({
            "confirmed_dialogue":dialogue,"current_speaker":event.user_id,"latest_text":event.text,
            "member_own_memories":own_memories},ensure_ascii=False)})
        trace_data["dialogue_count"] = len(dialogue)
        try:
            async with self._capacity:
                if not current():
                    return quiet("followup_policy_changed")
                # Capacity waiting is not a paid attempt. A replaced candidate
                # must not spend a request or move the next call's timestamp.
                thread["last_attempt"]=time.monotonic()
                answer=await self._complete_traced(None,messages,settings,kind="followup",
                    trigger="followup",scope=scope,user=event.user_id,refs=trace_data,context_generation=context_generation,
                    image_event_id=event.event_id,request_text=event.text)
        except ProviderError as exc:
            if str(exc) in {"bot_stopped","cloud_budget","context_budget_exceeded","context_retry_budget","privacy_context_changed"}:
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
        if answer.get("_voting_invalid"):
            return quiet("voting_invalid")
        if answer.get("_participation_vote") and answer.get("continuation") is True and answer.get("participate") is not True:
            return quiet("followup_low_confidence")
        if not isinstance(answer,dict) or answer.get("continuation") is not True or answer.get("participate") is not True:
            self._group_threads.pop(key,None)
            self.dialogue.end_followup(scope,event.user_id)
            return quiet("followup_not_for_bot")
        confidence=answer.get("confidence")
        if type(confidence) not in (int,float) or not 0<=confidence<=1:
            return quiet("followup_confidence_invalid")
        if confidence<settings.group_chat_confidence_threshold:
            return quiet("followup_low_confidence")
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

    async def _handle_group(self, event, scope, settings, reply_id, *, demo=False):
        def quiet(reason):
            return self._finish_group(event,scope,reply_id,{"mode":"none","text":"","reason":reason},demo=demo)
        command = event.text.strip().split(maxsplit=1)[0] if event.text.strip() else ""
        if event.text.strip() in {"/旁听 关闭","/记忆 关闭","/忘记我"}:
            # Privacy withdrawal must invalidate an in-flight decision now, not
            # queue behind a potentially slow cloud call holding the group lock.
            owner=settings.is_owner(event.kind,event.scope_id,event.user_id,event.transport)
            answer=self.command(event,scope,event.text.strip(),owner)
            identity=(scope,event.user_id)
            if not demo and time.monotonic()-self._last.get(identity,0)<settings.min_interval_seconds:
                return quiet("rate_limited")
            self._last[identity]=time.monotonic()
            return self._finish_group(event,scope,reply_id,{"mode":"text","text":answer},demo=demo)
        previous = self._group_snapshot(scope,settings)
        quoted_bot = bool(event.reply_to_id and any(r["role"]=="assistant" and r["message_id"]==event.reply_to_id for r in previous))
        direct = not event.unprompted or event.mentioned or command in GROUP_COMMANDS or quoted_bot
        addressed_elsewhere=not direct and (event.other_mentioned or (event.reply_to_id and not quoted_bot))
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
        # Commands and suspected credentials never enter the shared transcript.
        if command not in GROUP_COMMANDS and len(event.text)<=settings.max_input_chars:
            version = self.groups.add(scope,event.user_id,event.text,event.event_id,display_name=event.display_name)
            if (event.images and settings.vision_enabled and (version is not None or direct)
                and len(event.text)<=settings.max_input_chars and not GroupConversation.contains_credentials(event.text)
                and not event.text.lstrip().startswith("/")):
                privacy_generation=self.generation(scope)
                await self.vision.prepare(scope,event.user_id,event.event_id,event.images)
                if privacy_generation!=self.generation(scope) or not self._group_allowed_now(event,demo=demo):
                    return quiet("privacy_context_changed")
            if version is not None:
                self.memory.add_history(scope,event.user_id,"user",event.text,event.event_id)
                if self.memory.consent(scope,event.user_id):
                    member_lock = self._locks.setdefault((hash((scope,event.user_id))%64,),asyncio.Lock())
                    async with member_lock, self._capacity:
                        if (self._group_allowed_now(event,demo=demo) and not self.groups.opted_out(scope,event.user_id)
                            and self.memory.consent(scope,event.user_id)):
                            await self.summarize(scope,event.user_id,None,settings)
        if addressed_elsewhere:
            return quiet("followup_not_for_bot")
        debounced_current=True
        if not direct:
            if version is None:
                return quiet("observation_excluded")
            if not named:
                debounced_current=await self.groups.debounced(scope,version,settings.group_chat_debounce_seconds)
        async with self.groups.lock(scope):
            if not self._group_allowed_now(event,demo=demo):
                return quiet("group_policy_changed")
            heat=self.groups.heat(scope,settings)
            heated=not named and not direct and not addressed_elsewhere and heat["eligible"] and self.groups.recent_user_event(scope,event.user_id,event.event_id)
            # Re-read after debounce AND lock acquisition: an earlier reply's
            # ACK may have opened this member's conversation while we waited.
            thread=self._active_thread(scope,event.user_id,self.settings(scope)) if not direct else None
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
            if not direct:
                latest=(self.groups.is_latest_user_event(scope,event.user_id,event.event_id)
                        if thread or named or heated else self.groups.is_current(scope,version))
                if not latest:
                    return quiet("merged_into_newer_event" if not debounced_current else "superseded")
            if named and self.groups.opted_out(scope,event.user_id):
                return quiet("observation_excluded")
            if direct:
                privacy_generation=self.generation(scope)
                identity=(scope,event.user_id)
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
                    result={"mode":"none","text":"","reason":str(exc) if str(exc) in {"bot_stopped","cloud_budget","context_budget_exceeded","context_retry_budget","privacy_context_changed"} else "cloud_failure"}
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
                return await self._continue_group_thread(event,scope,settings,reply_id,thread,demo=demo)
            last_cutoff,last_at=self._group_decisions.get(scope,(0.,0.))
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
            interval=settings.group_heat_interval_seconds if heated else settings.group_chat_decision_interval_seconds
            if not named and time.monotonic()-last_at<interval:
                return quiet("decision_cooldown")
            context=self._group_snapshot(scope,settings)
            new_messages=sum(r["role"]=="user" and r["at"]>last_cutoff for r in context)
            if not named and not heated and new_messages<settings.group_chat_min_messages and not re.search(r"[?？]",event.text):
                return quiet("collecting_context")
            budget=self._group_budget(settings)
            if heated:
                budget["cooldown_seconds"]=settings.group_heat_interval_seconds
            if not named and not self.groups.budget_status(scope,**budget)["allowed"]:
                return quiet("group_speaking_budget")
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
                if str(exc) in {"bot_stopped","cloud_budget","context_budget_exceeded","context_retry_budget","privacy_context_changed"}:
                    self.provider_failure(exc,settings)
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
                if named:
                    return named_current()
                if not heated:
                    return self.groups.is_current(scope,version)
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
            if answer.get("_voting_invalid"):
                return quiet("voting_invalid")
            if not isinstance(answer,dict) or answer.get("participate") is not True:
                return quiet("low_confidence" if answer.get("_participation_vote") else "chose_silence")
            confidence=answer.get("confidence")
            if type(confidence) not in (int,float) or not settings.group_chat_confidence_threshold<=confidence<=1:
                return quiet("low_confidence")
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

    def _group_prompt(self,event,scope,settings,context,*,trace=None,memories=None,addressed=False,heat=None,dialogue=None):
        policy=prompt(self.content_root,"group",settings)
        policy += "\n" + "\n".join(prompt(self.content_root,key,settings) for key in ("knowledge","expression","vision","output","style","dialogue"))
        memories = self.memory.retrieve(scope,event.user_id,event.text) if memories is None else memories
        knowledge = lore.for_prompt(self.content_root,event.text)
        persona = (self.content_root/"content/persona.md").read_text()
        expressions = assets.catalog(self.content_root,event.text)
        dialogue=self._dialogue_context(event,scope,settings) if dialogue is None else dialogue
        data={"confirmed_direct_dialogue":dialogue,"current_message":self._current_message(event),
              "available_expressions":expressions,"recent_group_messages":context,"latest_speaker_id":event.user_id,
              "latest_speaker_familiarity":self.groups.familiarity(scope,event.user_id),
              "latest_speaker_own_memories":memories,
              "lore":knowledge}
        if heat is not None:
            data["group_activity"]=heat
            policy += "\n"+prompt(self.content_root,"heat",settings)
        names=getattr(self,"address_names_loader",lambda:[])()
        if addressed or is_addressed(event.text,names):
            data["addressed_candidate"]={"user_id":event.user_id,"message_id":event.event_id,"text":event.text}
            policy += prompt(self.content_root,"addressed",settings)
        if trace is not None:
            trace.update(trace_references(memories=memories,knowledge=knowledge,persona=persona,
                                          group_context=context,dialogue=dialogue,input_characters=len(event.text),source_event_id=event.event_id,expressions=expressions))
            trace["_text_preview_allowed"] = self._preview_context_allowed(scope,context)
            if heat is not None:trace["group_activity"]=heat
        return [{"role":"system","content":policy+prompt(self.content_root,"persona_header",settings)+persona+
                 prompt(self.content_root,"persona_footer",settings)},
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
        if not text:
            return {"mode": "none", "text": ""}
        if not self.is_active():
            return {"mode":"none","text":"","reason":"profile_changed"}
        # Observation is independent of a successful cloud response. Consent,
        # source deduplication and retention are enforced by the memory store.
        self.memory.add_history(scope,event.user_id,"user",text,event.event_id)
        if settings.provider_mode=="cloud" and time.monotonic()<self._cloud_retry_after:
            return {"mode":"none","text":"","reason":"cloud_backoff"}
        context_generation=self.generation(scope)
        memories = self.memory.retrieve(scope, event.user_id, text)
        knowledge = lore.for_prompt(self.content_root, text)
        # Durable history is for memory management, not implicit conversation
        # replay. Every current conversation uses only recent confirmed turns.
        history = []
        group_thread=self._dialogue_context(event,scope,settings) if group_thread is None else group_thread
        persona = (self.content_root / "content/persona.md").read_text()
        policy = prompt(self.content_root,"direct",settings)
        policy += "\n" + "\n".join(prompt(self.content_root,key,settings) for key in ("knowledge","expression","vision","output","style","dialogue"))
        expressions = [] if mode_override else assets.catalog(self.content_root,text)
        context = {"current_message":self._current_message(event),
                   "available_expressions":expressions,"trusted_identity": {"kind":event.kind, "user_id":event.user_id, "is_owner":owner},
                   "retrieved_memory": memories, "lore": knowledge}
        if group_context is not None:
            context.update(recent_group_messages=group_context,
                           member_familiarity=self.groups.familiarity(scope,event.user_id))
        if group_thread:
            context["confirmed_direct_dialogue"]=group_thread
        messages = [{"role":"system", "content":policy + prompt(self.content_root,"persona_header",settings) + persona +
                     prompt(self.content_root,"persona_footer",settings)},
                    {"role":"user", "content":prompt(self.content_root,"direct_header",settings) + json.dumps(context, ensure_ascii=False)}]
        messages.extend({"role": r["role"], "content": r["text"]} for r in history
                        if r["role"] in {"user","assistant"} and r["event_id"] != event.event_id)
        messages.append({"role":"user", "content":text})
        trace_data = trace_references(memories=memories,knowledge=knowledge,history=history,persona=persona,
                                      group_context=group_context,dialogue=group_thread,input_characters=len(text),
                                      source_event_id=event.event_id,expressions=expressions)
        trace_data["_text_preview_allowed"] = self._preview_context_allowed(scope,group_context)
        trace_id = self._trace_start(settings,"reply","direct",scope,event.user_id,trace_data,request_text=event.text)
        try:
            provider = self.providers(settings)
        except Exception as exc:
            self.trajectories.finish(trace_id,"failed",error=exc)
            raise
        answer = await self._complete_traced(provider,messages,settings,kind="reply",trigger="direct",
                                            scope=scope,user=event.user_id,refs=trace_data,trace_id=trace_id,context_generation=context_generation,
                                            image_event_id=event.event_id,history_events={r["id"]:r["event_id"] for r in history},
                                            request_text=event.text)
        trace_id = trace_data.pop("_trace_id",trace_id)
        if not self.is_active():
            return {"mode":"none","text":"","reason":"profile_changed"}
        if context_generation!=self.generation(scope):
            return {"mode":"none","text":"","reason":"privacy_context_changed"}
        if not isinstance(answer, dict):
            raise ProviderError("invalid_reply_object")
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
        if self.memory.consent(scope, event.user_id):
            await self.summarize(scope, event.user_id, provider, settings)
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
                            context_generation,provider=None,mode_override=None,related_id=None,valid=None):
        """One output boundary for direct replies, named turns and follow-ups.

        This only prepares media; the bridge remains the sole sender. Any
        generation-time revocation cancels both audio and fallback text.
        """
        def current():
            return (self.is_active() and context_generation==self.generation(scope)
                    and (valid is None or valid()))

        def quiet():
            return {"mode":"none","text":"","reason":"privacy_context_changed" if self.is_active() else "bot_stopped"}

        if not current():
            return quiet()
        if answer.get("_web_evidence"):
            return {"mode":"text","text":web_search.cite(dict(answer,text=response),answer["_web_evidence"],show=settings.web_search_show_sources or web_search.wants_sources(event.text))["text"]}
        mode=mode_override or answer.get("mode","text")
        result=({"mode":"text","text":response} if mode_override else
                self._expression_reply(answer,response,trace_data.get("assets",[])))
        if (result["mode"]=="image" or mode!="voice" or not settings.voice_enabled
            or not settings.voice_authorized or len(response)>settings.max_voice_chars
            or re.search(r"https?://|```",response)):
            return result

        voice_trace=None
        try:
            # Recheck effective Profile voice settings before beginning a new
            # billable operation. GUI edits also invalidate the generation.
            latest=self.settings()
            if not current():
                return quiet()
            if not latest.voice_enabled or not latest.voice_authorized:
                return result
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
            path=await provider.synthesize(response)
            result={"mode":"voice","text":response,"media_path":str(path)}
            latest=self.settings()
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
            self.diagnostics.record("tts",safe_code(exc))
            log.warning("voice_generation_failed")
            # Synthesis failed before sending anything: keep one actual text
            # reply. Never append a delivery placeholder or retry a QQ send.
            return {"mode":"text","text":response}
        except Exception as exc:
            if voice_trace is not None:
                self.trajectories.finish(voice_trace,"failed",error=exc)
            self.discard_generated_media(result)
            raise

    async def summarize_due(self):
        """One fair, bounded background extraction in an enabled live scope."""
        settings=self.settings()
        if not self.is_active():
            return {"status":"profile_changed","saved":0}
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

    async def summarize(self, scope: str, user: str, provider, settings: Settings, *, force=False, trigger=None):
        # Snapshot this profile/group's cadence for both event and timer paths.
        settings=Settings.model_validate(settings.model_dump() | self.memory.summary_policy(scope))
        state = read_control(self.root)
        if not state["enabled"]:
            return {"status": "bot_stopped", "saved": 0}
        lease = self._activity_epoch.set(self._activity_epoch.get() or state["epoch"])
        try:
            return await self._summarize(scope, user, provider, settings, force=force, trigger=trigger)
        finally:
            self._activity_epoch.reset(lease)

    async def _summarize(self, scope: str, user: str, provider, settings: Settings, *, force=False, trigger=None):
        if not self.is_active():
            return {"status":"profile_changed","saved":0}
        context_generation = self.generation(scope)
        claim=self.memory.begin_summary(scope,user,threshold=settings.auto_summary_every,force=force,
                                        max_wait_seconds=settings.auto_summary_max_wait_minutes*60)
        if claim["status"] != "ready":
            return claim
        batch=claim["batch"]
        pending=batch["history"]
        trace_data = trace_references(history=pending)
        trigger = "manual" if force else trigger or "automatic"
        trace_id = self._trace_start(settings,"summary",trigger,scope,user,trace_data,
            request_text=pending[-1]["text"] if pending else None,request_kind="summary_source")
        if settings.provider_mode == "cloud" and time.monotonic() < self._cloud_retry_after:
            self.trajectories.finish(trace_id,"cloud_backoff")
            return self.memory.fail_summary(scope,user,batch,"cloud_backoff",max(60,round(self._cloud_retry_after-time.monotonic())))
        try:
            # Most observed messages do not yet form a due batch. Resolve
            # credentials only once a real extraction is ready and budgeted.
            data = await self._complete_traced(provider,[
                {"role":"system", "content":prompt(self.content_root,"summary",settings)},
                {"role":"user", "content":json.dumps(pending, ensure_ascii=False)}],settings,
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
            result=self.memory.finish_summary(scope,user,batch,data.get("memories",[]))
            if result["status"] in {"saved","no_facts"}:
                summary_preview=[{"text":item["text"],"source_ids":[n for n in item.get("source_ids",[])[:100] if type(n) is int]}
                    for item in data.get("memories",[])[:3] if isinstance(item,dict) and isinstance(item.get("text"),str)
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
            return ("白名单群开启群聊参与时，最近图文会作为短期上下文发往已配置的云端模型判断是否接话；图片理解可由管理员关闭，图片只在内存短期缓存，GIF仅处理首帧。群短期上下文不整段落盘；本机调用轨迹另保存触发消息末尾最多120字和回复最多2000字供管理员排查，随轨迹保留最多30天，删除/纠错来源会清理相关预览。可用 /旁听 关闭 退出并停止保存本人新的轨迹文本。\n"
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
