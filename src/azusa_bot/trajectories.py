"""Bounded records of actual provider calls and recognizable reply previews.

The active profile's memory database owns these records, so normal backups and
privacy erasure share a transaction. The administrator requested a short tail
of the triggering message and cleaned reply, never a full prompt, reasoning,
HTTP response or group transcript. Source previews remain fingerprint-bound.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import lore
from .diagnostics import safe_code, CANCELLATION_REASONS
from .group_chat import GroupConversation
from .provider_usage import safe_usage
from .web_search import public_url, public_query
from .group_topic import safe_topic
from .providers import ProviderError

KINDS = {"reply", "group_idle", "group_idle_manual", "group_decision", "followup", "summary", "tts", "web_search", "group_topic"}
TRIGGERS = {"direct", "group_observation", "followup", "automatic", "background", "manual"}
STATUSES = {"running", "success", "failed", "saved", "no_facts", "superseded",
            "interrupted", "invalid_output", "cloud_budget", "cloud_backoff", "profile_changed", "research_requested", "topic_blocked"}
REQUEST_TAIL_CHARS = 120
RESPONSE_PREVIEW_CHARS = 2000
REDACTED = "[已隐藏可能包含凭据的内容]"


def safe_exchange_text(text, limit, *, tail=False):
    """Inspect the complete value before slicing so a secret suffix cannot leak."""
    if not isinstance(text, str):
        return None, False
    if (GroupConversation.contains_credentials(text)
            or re.search(r"(?i)(?:\bSUBP?|\bSESSDATA|\bbili_jct|\bCookie|\bXSRF-TOKEN)\s*[:=]\s*\S+", text)
            or re.search(r"\bAIza[A-Za-z0-9_-]{20,}", text)):
        return REDACTED, False
    text = text.replace("\x00", "")
    return (text[-limit:] if tail else text[:limit]), len(text) > limit


def public_exchange(value):
    """Stable fields for old records and a defensive bound at the API boundary."""
    result = {"request_tail": None, "request_truncated": False, "request_kind": None,
              "response_text": None, "response_truncated": False, "response_kind": None,
              "preview_redacted": value.get("redacted") is True}
    if result["preview_redacted"]:
        return result
    for prefix, key, limit in (("request", "request_tail", REQUEST_TAIL_CHARS),
                               ("response", "response_text", RESPONSE_PREVIEW_CHARS)):
        text, truncated = safe_exchange_text(value.get(key), limit, tail=prefix == "request")
        result[key] = text
        result[prefix+"_truncated"] = truncated or value.get(prefix+"_truncated") is True
        kind = value.get(prefix+"_kind")
        allowed = {"message", "summary_source", "speech"} if prefix == "request" else {"reply", "summary", "speech"}
        result[prefix+"_kind"] = kind if kind in allowed else None
    return result


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def safe_preview(text: str, limit=360) -> str:
    if GroupConversation.contains_credentials(str(text)):
        return "[已隐藏可能包含凭据的内容]"
    return str(text)[:limit]


def model_label(value) -> str:
    value = str(value or "")
    if (not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", value)
            or GroupConversation.contains_credentials(value)):
        return "configured_model"
    return value


def memory_fingerprint(row):
    return fingerprint({key: row.get(key) for key in
                        ("id", "kind", "source", "text", "source_ids", "updated_at")})


def knowledge_fingerprint(row):
    return fingerprint({key: row.get(key) for key in
                        ("doc_id", "title", "category", "sources", "text", "start_char", "end_char")})


def safe_image_references(images):
    """Keep bounded visual provenance only, never an image/URL/caption archive.

    Incoming picture bytes live in the ephemeral cache. Even if callers pass
    whole cache rows here, persistence only receives these selected fields.
    Invalid references are omitted rather than converted to a fake source.
    """
    if not isinstance(images, (list, tuple)):
        return []
    result = []
    for row in images[:12]:
        if (not isinstance(row, dict) or not isinstance(row.get("image_id"), str)
                or not re.fullmatch(r"[a-f0-9]{32,64}", row["image_id"])):
            continue
        item = {"image_id": row["image_id"]}
        for key in ("event_id", "user_id"):
            value = str(row.get(key, ""))
            item[key] = (value if re.fullmatch(r"-?[0-9]{1,20}", value) else
                         value if re.fullmatch(r"sha256:[a-f0-9]{64}", value) else
                         "sha256:" + fingerprint(value) if value else "")
        status = row.get("status")
        item["status"] = status if isinstance(status, str) and status in {"ready", "unavailable", "expired"} else "unavailable"
        code = row.get("error_code", "")
        item["error_code"] = code if isinstance(code, str) and code in IMAGE_ERROR_CODES else "vision_unavailable"
        for key in ("width", "height"):
            value = row.get(key)
            if type(value) is int and 0 < value <= 8192:
                item[key] = value
        item["animated_first_frame"] = row.get("animated_first_frame") is True
        if isinstance(row.get("relation"),str) and row["relation"] in {"current","recent","reply"}:
            item["relation"] = row["relation"]
        age = row.get("age_seconds")
        if type(age) is int and 0 <= age <= 7200:
            item["age_seconds"] = age
        result.append(item)
    return result


IMAGE_ERROR_CODES = frozenset({"", "vision_unavailable", "vision_url_rejected", "vision_download_failed",
    "vision_image_too_large", "vision_invalid_image", "vision_cache_full", "vision_expired",
    "vision_context_changed", "vision_disabled", "vision_http_error", "vision_fetch_failed",
    "vision_unsupported_format", "vision_image_invalid", "vision_image_dimensions",
    "vision_busy", "vision_context_removed", "vision_dimensions_exceeded",
    "vision_format_unsupported", "vision_model_unsupported"})


def safe_web_reference(value,redacted=False):
    if not isinstance(value,dict):
        return None
    if redacted:
        return {"status":"redacted","sources":[]}
    status=value.get("status")
    result={"status":status if isinstance(status,str) and re.fullmatch(r"[a-z_0-9]{1,70}",status) else "unknown","sources":[]}
    try:
        result["query"]=public_query(value.get("query"))
    except ProviderError:
        pass
    at=value.get("searched_at")
    if type(at) in (int,float) and 0<=at<1e11:
        result["searched_at"]=at
    sources=value.get("sources",[])
    for row in sources[:3] if isinstance(sources,list) else []:
        if not isinstance(row,dict) or not (url:=public_url(row.get("url"))):
            continue
        result["sources"].append({"id":len(result["sources"])+1,"url":url,
            "title":safe_preview(row.get("title",""),160),"published_at":safe_preview(row.get("published_at",""),50),
            "content_kind":"page_excerpt" if row.get("content_kind")=="page_excerpt" else "search_summary"})
    return result


def _safe_refs(refs,redacted=False):
    result=dict(refs, images=safe_image_references(refs.get("images", [])))
    if redacted:
        result.pop("knowledge_lookup",None);result.pop("role_activity",None);result.pop("history_lookup",None)
    result.pop("_topic_rows",None)
    if "group_topic" in result:result["group_topic"]=safe_topic(result["group_topic"],redacted)
    result.pop("prompt_prefixes",None)
    result.pop("_prepared_messages",None);result.pop("_trace_id",None)
    if "web_search" in result:
        result["web_search"]=safe_web_reference(result["web_search"],redacted)
    return result


def references(*, memories=(), knowledge=(), history=(), persona=None,
               group_context=(), dialogue=(), input_characters=0, source_event_id="", expressions=(), images=()):
    """Use the already assembled prompt's sources, never run retrieval again."""
    return {
        "images": safe_image_references(images),
        "assets": [{"id": row["id"], "revision": row["revision"], "name": safe_preview(row["name"],80)} for row in expressions[:20]],
        "memories": [{"id": row["id"], "sha256": memory_fingerprint(row)} for row in memories[:50]],
        "knowledge": [{"doc_id": row["doc_id"], "chunk_id": row["chunk_id"],
                       "sha256": knowledge_fingerprint(row), "start_char": row["start_char"],
                       "end_char": row["end_char"], "always_include": row.get("always_include") is True,
                       "selection_reason":row.get("selection_reason","retrieved"),
                       "context_until":row.get("context_until",0)} for row in knowledge[:50]],
        "history": [{"id": row["id"], "sha256": fingerprint(row["text"])} for row in history[:200]],
        "persona": None if persona is None else {"sha256": fingerprint(persona), "characters": len(persona)},
        "group_context_count": len(group_context or ()), "dialogue_count": len(dialogue or ()),
        "input_characters": max(0, int(input_characters)),
        "source_event_id": (source_event_id if re.fullmatch(r"-?[0-9]{1,20}", str(source_event_id))
                            else "sha256:"+fingerprint(str(source_event_id)) if source_event_id else ""),
    }


class TrajectorySourceErased(RuntimeError):
    """A linked request may not recreate a deleted invocation."""


class TrajectoryStore:
    RETENTION_DAYS = 30
    MAX_RECORDS = 2000

    def __init__(self, memory):
        self.memory = memory
        self._db, self._lock = memory._db, memory._lock
        with self._lock, self._db:
            self._db.executescript("""
              CREATE TABLE IF NOT EXISTS trajectories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                trigger TEXT NOT NULL, scope TEXT NOT NULL, user_id TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, status TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL,
                model TEXT NOT NULL, provider_called INTEGER NOT NULL DEFAULT 0,
                refs TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', related_id INTEGER
              );
              CREATE INDEX IF NOT EXISTS trajectory_member ON trajectories(scope,user_id,id);
              CREATE INDEX IF NOT EXISTS trajectory_kind ON trajectories(kind,id);
            """)
            if "preview" not in {row[1] for row in self._db.execute("PRAGMA table_info(trajectories)")}:
                self._db.execute("ALTER TABLE trajectories ADD COLUMN preview TEXT NOT NULL DEFAULT '{}'")
            self._db.execute("UPDATE trajectories SET status='interrupted',finished_at=? WHERE status='running'",
                             (time.time(),))
            self._prune()

    def _prune(self):
        self._db.execute("DELETE FROM trajectories WHERE started_at<?", (time.time()-self.RETENTION_DAYS*86400,))
        self._db.execute("DELETE FROM trajectories WHERE id NOT IN (SELECT id FROM trajectories ORDER BY id DESC LIMIT ?)",
                         (self.MAX_RECORDS,))

    def begin(self, kind, trigger, scope, user, provider, model, refs, *, related_id=None,
              request_text=None, request_kind="message", preview_allowed=True):
        if kind not in KINDS or trigger not in TRIGGERS:
            raise ValueError("trajectory_type_invalid")
        self.memory._key(scope); self.memory._key(user)
        provider = provider if provider in {"deepseek", "openai_compatible", "compatible", "gemini_balance", "fish", "mock"} else "configured_provider"
        with self._lock, self._db:
            related = None
            if related_id is not None:
                related = self._db.execute("SELECT preview FROM trajectories WHERE id=? AND scope=? AND user_id=?",
                    (related_id,scope,user)).fetchone()
                if related is None:
                    raise TrajectorySourceErased()
            # Explicit member withdrawal also applies to future audit snippets.
            consent = self._db.execute("SELECT enabled FROM consent WHERE scope=? AND user_id=?", (scope,user)).fetchone()
            inherited_redaction = related and json.loads(related["preview"]).get("redacted") is True
            preview = {"redacted": True} if not preview_allowed or (consent and not consent[0]) or inherited_redaction else {}
            if not preview.get("redacted") and isinstance(request_text, str):
                text, truncated = safe_exchange_text(request_text, REQUEST_TAIL_CHARS, tail=True)
                preview.update(request_tail=text, request_truncated=truncated,
                               request_kind=request_kind if request_kind in {"message","summary_source","speech"} else "message")
            number = self._db.execute("""INSERT INTO trajectories
                (kind,trigger,scope,user_id,started_at,status,provider,model,refs,related_id,preview)
                VALUES (?,?,?,?,?,'running',?,?,?,?,?)""",
                (kind, trigger, scope, user, time.time(), provider, model_label(model),
                 json.dumps(_safe_refs(refs,preview.get("redacted") is True), ensure_ascii=False), related_id, json.dumps(preview,ensure_ascii=False))).lastrowid
            self._prune()
            return number

    def record_response(self, number, text, *, kind="reply", source_text=None):
        """Keep the cleaned, visible model output; never revive erased snippets."""
        if not isinstance(text,str) or kind not in {"reply","summary","speech"}:
            return False
        with self._lock, self._db:
            row = self._db.execute("SELECT preview FROM trajectories WHERE id=? AND provider_called=1", (number,)).fetchone()
            if row is None:
                return False
            preview = json.loads(row["preview"])
            if preview.get("redacted"):
                return False
            text, truncated = safe_exchange_text(text, RESPONSE_PREVIEW_CHARS)
            if isinstance(source_text,str) and safe_exchange_text(source_text,RESPONSE_PREVIEW_CHARS)[0] == REDACTED:
                text, truncated = REDACTED, False
            preview.update(response_text=text,response_truncated=truncated,response_kind=kind)
            self._db.execute("UPDATE trajectories SET preview=? WHERE id=?", (json.dumps(preview,ensure_ascii=False),number))
            return True

    def set_request_preview(self, number, text, *, kind="message"):
        if not isinstance(text,str) or kind not in {"message","summary_source","speech"}:
            return False
        with self._lock, self._db:
            row=self._db.execute("SELECT preview FROM trajectories WHERE id=?",(number,)).fetchone()
            if row is None:
                return False
            preview=json.loads(row["preview"])
            if preview.get("redacted"):
                return False
            text,truncated=safe_exchange_text(text,REQUEST_TAIL_CHARS,tail=True)
            preview.update(request_tail=text,request_truncated=truncated,request_kind=kind)
            self._db.execute("UPDATE trajectories SET preview=? WHERE id=?",(json.dumps(preview,ensure_ascii=False),number))
            return True

    def erase_previews(self, *, scope=None, knowledge_id=None):
        """Remove text copies when source data changes, keeping invocation metadata.

        Scope-wide erasure is deliberate: a group response can quote another
        member or a corrected fact even when its trigger came from someone else.
        Linked TTS/retry records inherit a referenced document's erasure.
        """
        if scope is None and knowledge_id is None:
            raise ValueError("trajectory_preview_erasure_requires_scope_or_source")
        with self._lock:
            # Memory changes call this within their existing erasure transaction.
            with (nullcontext() if self._db.in_transaction else self._db):
                rows = self._db.execute("SELECT id,refs,related_id FROM trajectories"+
                    (" WHERE scope=?" if scope is not None else ""), (scope,) if scope is not None else ()).fetchall()
                selected = {row["id"] for row in rows if knowledge_id is None or any(
                    ref.get("doc_id") == knowledge_id for ref in json.loads(row["refs"]).get("knowledge",[]))}
                while True:
                    expanded = selected | {row["id"] for row in rows if row["related_id"] in selected}
                    if expanded == selected:
                        break
                    selected = expanded
                sources={row["id"]:row["refs"] for row in rows}
                for number in selected:
                    refs=json.loads(sources[number])
                    self._db.execute("UPDATE trajectories SET preview=?,refs=? WHERE id=?",
                        ('{"redacted":true}',json.dumps(_safe_refs(refs,True)),number))
                return len(selected)

    def exists(self, number):
        with self._lock:
            return self._db.execute("SELECT 1 FROM trajectories WHERE id=?",(number,)).fetchone() is not None

    def set_context(self, number, refs):
        """Replace planned references with the exact bounded request, never resurrect an erased call."""
        with self._lock,self._db:
            row=self._db.execute("SELECT preview FROM trajectories WHERE id=?",(number,)).fetchone()
            redacted=row and json.loads(row["preview"]).get("redacted") is True
            return bool(self._db.execute("UPDATE trajectories SET refs=? WHERE id=? AND status='running'",
                (json.dumps(_safe_refs(refs,redacted),ensure_ascii=False),number)).rowcount)

    def called(self, number):
        with self._lock, self._db:
            self._db.execute("UPDATE trajectories SET provider_called=1 WHERE id=?", (number,))

    def record_usage(self, number, usage):
        """Attach returned counts only to an existing, actually called request."""
        usage = safe_usage(usage)
        if not usage:
            return False
        with self._lock, self._db:
            row = self._db.execute("SELECT result FROM trajectories WHERE id=? AND provider_called=1", (number,)).fetchone()
            if row is None:
                return False
            data = json.loads(row["result"])
            data["usage"] = usage
            self._db.execute("UPDATE trajectories SET result=? WHERE id=?", (json.dumps(data), number))
            return True

    def finish(self, number, status="success", *, error=None, result=None, cancellation_reason=None):
        status = status if status in STATUSES else "failed"
        data = {}
        if cancellation_reason in CANCELLATION_REASONS:
            data["cancellation_reason"] = cancellation_reason
        if isinstance(result, dict):
            from .participation_vote import safe_result
            vote=safe_result(result.get("_participation_vote"))
            if vote is not None:
                data["participation_vote"]=vote
            fields={"participate","confidence","continuation",*("votes."+k for k in ("relevance","understanding","contribution","timing"))}
            errors=result.get("_contract_errors")
            if isinstance(errors,list):data["contract_errors"]=[v for v in errors if isinstance(v,str) and v in fields]
            for key in ("processed", "saved", "output_characters"):
                value = result.get(key)
                if type(value) is int and 0 <= value <= 100000:
                    data[key] = value
            ids = result.get("memory_ids", result.get("output_memory_ids", []))
            if isinstance(ids, list):
                data["output_memory_ids"] = [n for n in ids[:3] if type(n) is int and n > 0]
            for key in ("participate", "continuation"):
                if type(result.get(key)) is bool:
                    data[key] = result[key]
            if result.get("mode") in {"text", "voice", "image", "none"}:
                data["mode"] = result["mode"]
        with self._lock, self._db:
            row = self._db.execute("SELECT result,kind FROM trajectories WHERE id=?", (number,)).fetchone()
            if row is None:
                return
            if isinstance(result,dict) and row["kind"] in {"followup", "group_decision", "group_idle", "group_topic"}:
                confidence = result.get("confidence")
                valid = type(confidence) in (int,float) and 0<=confidence<=1
                data["confidence_state"] = "valid" if valid else "invalid" if "confidence" in result else "missing"
                if valid:
                    data["confidence"] = confidence
            # Summary extraction finishes after the model request returns.
            # Its result/status must not discard already recorded usage.
            usage = safe_usage(json.loads(row["result"]).get("usage"))
            if usage:
                data["usage"] = usage
            # An erasure while the provider was running must never recreate it.
            self._db.execute("UPDATE trajectories SET finished_at=?,status=?,error_code=?,result=? WHERE id=?",
                (time.time(), status, safe_code(error) if error is not None else "", json.dumps(data), number))

    @staticmethod
    def _public(row):
        row = dict(row)
        exchange = public_exchange(json.loads(row.pop("preview", "{}")))
        refs, result = json.loads(row.pop("refs")), json.loads(row.pop("result"))
        row["usage"] = safe_usage(result.pop("usage", None)) or None
        row["cancellation_message"] = CANCELLATION_REASONS.get(result.get("cancellation_reason"))
        row["user"] = row.pop("user_id")
        row["provider_called"] = bool(row["provider_called"])
        row["source_event_id"] = refs.get("source_event_id", "")
        key = refs.get("delivery_event_key")
        row["idle_attempt_id"] = refs.get("idle_attempt_id")
        row["delivery_event_key"] = key if isinstance(key,str) and re.fullmatch(r"[a-f0-9]{64}",key) else None
        row["context_budget"] = refs.get("context_budget")
        row["request_layout"] = refs.get("request_layout")
        row["reply_contract"] = refs.get("reply_contract")
        row["effective_group_settings"] = refs.get("effective_group_settings")
        row["group_handoff"] = refs.get("group_handoff")
        row["prompt_revision"] = refs.get("prompt_revision")
        row["begin_dialogs"] = refs.get("begin_dialogs")
        row["execution"] = refs.get("execution")
        row["group_topic"] = safe_topic(refs.get("group_topic"),exchange["preview_redacted"])
        row["group_activity"] = refs.get("group_activity")
        row["confidence_policy"] = refs.get("confidence_policy")
        row["web_search"] = safe_web_reference(refs.get("web_search"),exchange["preview_redacted"])
        row['role_activity']=None if exchange['preview_redacted'] else refs.get('role_activity')
        row['recent_situation']=None if exchange['preview_redacted'] else refs.get('recent_situation')
        row['history_lookup']=None if exchange['preview_redacted'] else {k:v for k,v in refs.get('history_lookup',{}).items() if k!='references'} or None
        row['knowledge_lookup']=None if exchange['preview_redacted'] else refs.get('knowledge_lookup')
        threshold=refs.get("confidence_threshold")
        row["confidence_threshold"] = threshold if type(threshold) in (int,float) and 0<=threshold<=1 else None
        row["counts"] = {"images": len(safe_image_references(refs.get("images", []))), "memories": len(refs["memories"]), "knowledge": len(refs["knowledge"]),
                         "history": len(refs["history"]), "history_lookup":len(refs.get("history_lookup",{}).get("references",[])), "group_context": refs["group_context_count"],
                         "dialogue": refs["dialogue_count"]}
        return dict(row, processed=result.pop("processed", 0), saved=result.pop("saved", 0),
                    output_memory_ids=result.pop("output_memory_ids", []), **result, **exchange)

    def list(self, *, scope="", user="", kind="", limit=50, before=None):
        self.memory._limit(limit, 100)
        if kind and kind not in KINDS:
            raise ValueError("trajectory_type_invalid")
        clauses, args = [], []
        for field, value in (("scope", scope), ("user_id", user), ("kind", kind)):
            if value:
                self.memory._key(value)
                clauses.append(field+"=?"); args.append(value)
        if before is not None:
            if type(before) is not int or before < 1:
                raise ValueError("trajectory_cursor_invalid")
            clauses.append("id<?"); args.append(before)
        with self._lock, self._db:
            self._prune()
            rows = self._db.execute("SELECT * FROM trajectories" +
                (" WHERE "+" AND ".join(clauses) if clauses else "")+" ORDER BY id DESC LIMIT ?", args+[limit]).fetchall()
            return {"items": [self._public(row) for row in rows], "retention_days": self.RETENTION_DAYS,
                    "max_records": self.MAX_RECORDS,
                    "next_before": rows[-1]["id"] if len(rows) == limit else None}

    def get(self, number, content_root: Path):
        with self._lock, self._db:
            self._prune()
            row = self._db.execute("SELECT * FROM trajectories WHERE id=?", (number,)).fetchone()
            if row is None:
                return None
            result, refs = self._public(row), json.loads(row["refs"])
            resolved = {"images": safe_image_references(refs.get("images", [])),
                        "memories": [], "knowledge": [], "history": [],
                        "group_context_count": refs["group_context_count"], "dialogue_count": refs["dialogue_count"],
                        "input_characters": refs["input_characters"]}
            for ref in refs["memories"]:
                item = {"id": ref["id"], "available": False}
                found = self._db.execute("SELECT * FROM memories WHERE id=? AND scope=? AND (subject=? OR subject IS NULL)",
                                        (ref["id"], row["scope"], row["user_id"])).fetchone()
                if found:
                    memory = self.memory._memory_record(found)
                    if memory_fingerprint(memory) == ref["sha256"]:
                        item.update(available=True, kind=memory["kind"], source=memory["source"],
                                    preview=safe_preview(memory["text"]), source_ids=memory["source_ids"])
                resolved["memories"].append(item)
            for ref in refs["history"]:
                item = {"id": ref["id"], "available": False}
                found = self._db.execute("SELECT text,role FROM history WHERE id=? AND scope=? AND user_id=?",
                                        (ref["id"], row["scope"], row["user_id"])).fetchone()
                if found and fingerprint(found["text"]) == ref["sha256"]:
                    item.update(available=True, role=found["role"], preview=safe_preview(found["text"]))
                resolved["history"].append(item)
        # Lore takes its own lock; do not invert the memory/lore lock order.
        for ref in refs["knowledge"]:
            item = {key: ref[key] for key in ("doc_id", "chunk_id", "start_char", "end_char")}
            item["available"] = False
            item["always_include"] = ref.get("always_include") is True
            item['selection_reason']=ref.get('selection_reason','retrieved')
            item['context_until']=ref.get('context_until',0)
            try:
                doc = lore.get_document(content_root, ref["doc_id"])
                excerpt = doc["text"][ref["start_char"]:ref["end_char"]]
                check = dict(doc, doc_id=ref["doc_id"], text=excerpt,
                             start_char=ref["start_char"], end_char=ref["end_char"])
                if knowledge_fingerprint(check) == ref["sha256"]:
                    safe_sources = []
                    for source in doc.get("sources", [])[:10]:
                        url = urlsplit(source)
                        if url.scheme in {"https", "http"} and url.hostname and not url.username and not url.password:
                            safe_sources.append(urlunsplit((url.scheme, url.netloc, url.path, "", "")))
                    item.update(available=True, title=safe_preview(doc["title"], 100), category=doc["category"],
                                sources=safe_sources, preview=safe_preview(excerpt))
            except (ValueError, OSError, TypeError):
                pass
            resolved["knowledge"].append(item)
        persona = refs["persona"]
        if persona is not None:
            persona = dict(persona, available=False)
            try:
                persona["available"] = fingerprint((content_root/"content/persona.md").read_text()) == persona["sha256"]
            except OSError:
                pass
        resolved["persona"] = persona
        from . import conversation_config
        example_ref = refs.get("begin_dialogs", {})
        if example_ref.get("pairs"):
            try:
                config = conversation_config.read(content_root)
                available = conversation_config.example_revision(config) == example_ref.get("revision")
            except (OSError, ValueError):
                available = False
            resolved["begin_dialogs"] = {**example_ref, "available":available,
                "messages":conversation_config.examples(config) if available else []}
        from .assets import resolve_references
        resolved["assets"] = resolve_references(content_root,refs.get("assets",[]))
        resolved["context_budget"] = refs.get("context_budget")
        resolved["web_search"] = result.get("web_search")
        result["references"] = resolved
        # Forget/delete may have run while lore was being resolved.
        with self._lock:
            current = self._db.execute("SELECT preview FROM trajectories WHERE id=?", (number,)).fetchone()
            if current is None:
                return None
            # Source erasure can remove only snippets while preserving the call.
            result.update(public_exchange(json.loads(current["preview"])))
            if result["preview_redacted"]:
                result["web_search"]=resolved["web_search"]=safe_web_reference(refs.get("web_search"),True)
                result["group_topic"]=safe_topic(refs.get("group_topic"),True)
                result["knowledge_lookup"]=result["role_activity"]=None
            # A source correction may have happened while we held the lore
            # lock. Revalidate before returning an earlier in-memory preview.
            for index, ref in enumerate(refs["memories"]):
                found = self._db.execute("SELECT * FROM memories WHERE id=? AND scope=? AND (subject=? OR subject IS NULL)",
                                        (ref["id"], row["scope"], row["user_id"])).fetchone()
                if not found or memory_fingerprint(self.memory._memory_record(found)) != ref["sha256"]:
                    resolved["memories"][index] = {"id": ref["id"], "available": False}
            for index, ref in enumerate(refs["history"]):
                found = self._db.execute("SELECT text FROM history WHERE id=? AND scope=? AND user_id=?",
                                        (ref["id"], row["scope"], row["user_id"])).fetchone()
                if not found or fingerprint(found["text"]) != ref["sha256"]:
                    resolved["history"][index] = {"id": ref["id"], "available": False}
        from .history_lookup import resolve
        resolved['requested_history']=[] if result['preview_redacted'] else resolve(self.memory,row['scope'],row['user_id'],refs.get('history_lookup',{}))
        if result['preview_redacted']:result['history_lookup']=None
        return result

    def erase_image_sources(self, scope, user):
        """Remove one member's picture identities from other members' traces.

        The enclosing profile owns this store. A cross-member visual reference
        must not survive forgetting its source just because a different member
        asked the follow-up question. This does not remove that other reply's
        remaining audit record or reach into another group.
        """
        self.memory._key(scope)
        self.memory._key(user)
        user = str(user)
        identity = user if re.fullmatch(r"-?[0-9]{1,20}", user) else "sha256:" + fingerprint(user)
        changed = 0
        with self._lock, self._db:
            rows = self._db.execute("SELECT id,refs FROM trajectories WHERE scope=?", (scope,)).fetchall()
            for row in rows:
                refs = json.loads(row["refs"])
                images = safe_image_references(refs.get("images", []))
                retained = [image for image in images if image["user_id"] != identity]
                if len(retained) != len(images):
                    refs["images"] = retained
                    changed += self._db.execute("UPDATE trajectories SET refs=? WHERE id=?",
                        (json.dumps(refs, ensure_ascii=False), row["id"])).rowcount
        return changed

    def delete(self, number):
        with self._lock, self._db:
            return bool(self._db.execute("DELETE FROM trajectories WHERE id=?", (number,)).rowcount)
