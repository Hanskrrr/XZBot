"""Small, local SQLite memory store with explicit tenant and member boundaries.

Identity and ``is_owner`` must come from the authenticated transport, never an
LLM, display name, request body, or text claiming to be the owner. The store has
no network access. Group collection can be enabled by the local administrator;
explicit member choices override that default. Private history stays opt-in.
An explicit ``add_memory`` is itself an instruction to store that one fact.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Any
from .message_privacy import hidden_message


class MemoryStore:
    """Thread-safe store. Each public operation is one SQLite transaction.

    Histories retain at most ``max_history`` messages per scoped member and
    ``history_days`` days. Inferred summaries may outlive their source text;
    immutable numeric source IDs remain as provenance. User deletion removes
    related derived summaries. Call ``close`` before replacing/restoring a DB.
    """

    def __init__(self, path: str | Path, *, max_history: int = 200, history_days: int = 30):
        if max_history < 1 or history_days < 1:
            raise ValueError("Retention limits must be positive")
        self.path = str(path)
        self.max_history = max_history
        self.history_days = history_days
        if self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            os.chmod(target, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO memory_meta VALUES ('schema_version', '1');
                CREATE TABLE IF NOT EXISTS consent (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL, enabled INTEGER NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY (scope, user_id)
                );
                CREATE TABLE IF NOT EXISTS group_collection (
                    scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_policies (
                    scope TEXT PRIMARY KEY,auto_summary_every INTEGER NOT NULL,
                    auto_summary_max_wait_minutes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                    user_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
                    event_id TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE (scope, user_id, role, event_id)
                );
                CREATE INDEX IF NOT EXISTS history_member ON history(scope, user_id, id);
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                    subject TEXT, actor TEXT NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, source TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope, subject, kind);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    history_id INTEGER NOT NULL, PRIMARY KEY(memory_id, history_id)
                );
                CREATE INDEX IF NOT EXISTS source_history ON memory_sources(history_id);
                CREATE TABLE IF NOT EXISTS memory_facets (
                    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                    facet TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                    scope TEXT NOT NULL, actor TEXT NOT NULL,
                    object_id INTEGER, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS summary_progress (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL,
                    through_id INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0,
                    claim_id TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT 'not_started',
                    last_attempt_at REAL, last_success_at REAL,
                    last_saved INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(scope,user_id)
                );
            """)
            from .member_notes import schema as member_notes_schema
            member_notes_schema(self._db)
            from .quote_origins import schema as quote_origins_schema
            quote_origins_schema(self._db)
        self._secure_sidecars()
        version = self._db.execute("SELECT value FROM memory_meta WHERE key='schema_version'").fetchone()[0]
        if version != "1":
            self._db.close()
            raise ValueError("Unsupported memory schema version; restore a compatible release")
        with self._db:
            self._db.execute("DELETE FROM history WHERE created_at<?", (time.time() - self.history_days * 86400,))
        from .trajectories import TrajectoryStore
        self.trajectories = TrajectoryStore(self)

    def _secure_sidecars(self) -> None:
        if self.path != ":memory:":
            for suffix in ("-wal", "-shm"):
                candidate = Path(self.path + suffix)
                if candidate.exists():
                    os.chmod(candidate, 0o600)

    @staticmethod
    def _key(value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid stable scope/member/event ID")
        return value

    @staticmethod
    def _text(value: str, *, maximum: int = 8000) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError("Text is empty or exceeds the configured length limit")
        return value.strip()

    @staticmethod
    def _limit(value: int, maximum: int = 500) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"Limit must be between 1 and {maximum}")
        return value

    def _audit(self, action: str, scope: str, actor: str, object_id: int | None = None) -> None:
        # Intentionally no text, old values, keys, prompts, or model responses.
        self._db.execute("INSERT INTO audit(action,scope,actor,object_id,created_at) VALUES (?,?,?,?,?)",
                         (action, scope, actor, object_id, time.time()))
        self._db.execute("DELETE FROM audit WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM audit)")

    def _consent(self, scope: str, user: str) -> bool:
        row = self._db.execute("SELECT enabled FROM consent WHERE scope=? AND user_id=?", (scope, user)).fetchone()
        if row is not None:
            return bool(row[0])
        row = self._db.execute("SELECT enabled FROM group_collection WHERE scope=?", (scope,)).fetchone()
        return bool(row and row[0])

    def collection_source(self, scope: str, user: str) -> str:
        """Explain the effective policy without inventing individual consent."""
        self._key(scope); self._key(user)
        with self._lock:
            row = self._db.execute("SELECT enabled FROM consent WHERE scope=? AND user_id=?", (scope, user)).fetchone()
            if row is not None:
                return "member_enabled" if row[0] else "member_disabled"
            row = self._db.execute("SELECT enabled FROM group_collection WHERE scope=?", (scope,)).fetchone()
            return "group_default" if row and row[0] else "disabled"

    @staticmethod
    def _group_scope(scope: str) -> str:
        if not isinstance(scope, str) or not re.fullmatch(r"(?:onebot|qq):[A-Za-z0-9_-]{1,160}:group:[A-Za-z0-9_-]{1,160}", scope):
            raise ValueError("group_collection_requires_group_scope")
        return scope

    def group_collection_enabled(self, scope: str) -> bool:
        self._key(scope)
        with self._lock:
            row = self._db.execute("SELECT enabled FROM group_collection WHERE scope=?", (scope,)).fetchone()
            return bool(row and row[0])

    def known_members(self, scope: str, limit: int = 500) -> list[str]:
        """Return only stable IDs; works when passive group speech is disabled."""
        self._key(scope); self._limit(limit)
        with self._lock:
            rows = self._db.execute("""
                SELECT user_id FROM consent WHERE scope=?
                UNION SELECT user_id FROM history WHERE scope=?
                UNION SELECT subject AS user_id FROM memories WHERE scope=? AND subject IS NOT NULL
                ORDER BY user_id LIMIT ?
            """, (scope, scope, scope, limit)).fetchall()
            return [row[0] for row in rows]

    def set_group_collection(self, scope: str, enabled: bool) -> None:
        """Change a group's default; member overrides and old records survive.

        Turning the default off pauses inherited capture/extraction. It is not
        an erasure operation. Retoggling cannot silently re-enrol opted-out
        members or revive summaries computed under an earlier policy version.
        """
        self._group_scope(scope)
        if type(enabled) is not bool:
            raise ValueError("group_collection_requires_boolean")
        with self._lock, self._db:
            self._set_group_collection(scope,enabled)

    def _set_group_collection(self,scope,enabled):
        old = self._db.execute("SELECT enabled FROM group_collection WHERE scope=?", (scope,)).fetchone()
        self._db.execute("INSERT INTO group_collection VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                         (scope, int(enabled), time.time()))
        if bool(old and old[0]) != enabled:
            self._db.execute("UPDATE summary_progress SET generation=generation+1,claim_id='',last_status='changed',retry_at=0 WHERE scope=?", (scope,))
        self._audit("group_collection_on" if enabled else "group_collection_off", scope, "local_admin")

    def summary_policy(self, scope):
        with self._lock:
            row=self._db.execute('SELECT auto_summary_every,auto_summary_max_wait_minutes FROM memory_policies WHERE scope=?',(scope,)).fetchone()
            return dict(row) if row else {}

    def configure_group_memory(self,scope,every,wait,enabled):
        """Commit cadence and collection together; preserve all member overrides."""
        self._group_scope(scope)
        if type(every) is not int or not 4<=every<=100 or type(wait) is not int or not 5<=wait<=1440 or type(enabled) is not bool:
            raise ValueError('invalid_memory_settings')
        with self._lock,self._db:
            self._db.execute('INSERT INTO memory_policies VALUES (?,?,?) ON CONFLICT(scope) DO UPDATE SET auto_summary_every=excluded.auto_summary_every,auto_summary_max_wait_minutes=excluded.auto_summary_max_wait_minutes',(scope,every,wait))
            if self.group_collection_enabled(scope)!=enabled:
                self._set_group_collection(scope,enabled)
            self._audit('group_memory_policy',scope,'local_admin')

    def consent(self, scope: str, user: str) -> bool:
        self._key(scope); self._key(user)
        with self._lock:
            return self._consent(scope, user)

    def set_consent(self, scope: str, user: str, enabled: bool) -> None:
        """Enable recording, or stop and erase history plus inferred summaries.

        Explicitly saved personal facts survive disabling collection. Use
        ``forget_me`` to remove those too. Call only for the authenticated user.
        """
        self._key(scope); self._key(user)
        if not isinstance(enabled, bool):
            raise ValueError("Consent must be a boolean")
        with self._lock, self._db:
            self._db.execute("INSERT INTO consent VALUES (?,?,?,?) ON CONFLICT(scope,user_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                             (scope, user, int(enabled), time.time()))
            if not enabled:
                self._invalidate_summary(scope, user)
                self.trajectories.erase_previews(scope=scope)
                self._db.execute("DELETE FROM history WHERE scope=? AND user_id=?", (scope, user))
                self._db.execute("DELETE FROM memories WHERE scope=? AND subject=? AND source='inferred'", (scope, user))
                self._db.execute("DELETE FROM trajectories WHERE scope=? AND user_id=?", (scope, user))
            self._audit("consent_on" if enabled else "consent_off", scope, user)

    def _prune(self, scope: str, user: str) -> None:
        # Expire inactive members too whenever any scoped member is processed.
        self._db.execute("DELETE FROM history WHERE created_at<?",
                         (time.time() - self.history_days * 86400,))
        self._db.execute("DELETE FROM history WHERE scope=? AND user_id=? AND id NOT IN (SELECT id FROM history WHERE scope=? AND user_id=? ORDER BY id DESC LIMIT ?)",
                         (scope, user, scope, user, self.max_history))

    def add_history(self, scope: str, user: str, role: str, text: str, event_id: str) -> int | None:
        """Record under the effective collection policy; deduplicate replays."""
        self._key(scope); self._key(user); self._key(event_id)
        if role not in ("user", "assistant"):
            raise ValueError("History role must be user or assistant")
        text = self._text(text, maximum=32000)
        if hidden_message(text):
            return None
        with self._lock, self._db:
            if not self._consent(scope, user):
                return None
            self._db.execute("INSERT OR IGNORE INTO history(scope,user_id,role,text,event_id,created_at) VALUES (?,?,?,?,?,?)",
                             (scope, user, role, text, event_id, time.time()))
            row = self._db.execute("SELECT id FROM history WHERE scope=? AND user_id=? AND role=? AND event_id=?",
                                   (scope, user, role, event_id)).fetchone()
            self._prune(scope, user)
            return row[0]

    def recent_history(self, scope: str, user: str, limit: int = 20) -> list[dict[str, Any]]:
        """Read one authenticated member's history, oldest first; never group-wide."""
        self._key(scope); self._key(user); self._limit(limit)
        with self._lock, self._db:
            self._prune(scope, user)
            rows = self._db.execute("SELECT * FROM history WHERE scope=? AND user_id=? ORDER BY id DESC LIMIT ?", (scope, user, limit)).fetchall()
            return [dict(row) for row in reversed(rows) if not hidden_message(row["text"])]

    def scope_stats(self, scope):
        self._key(scope)
        with self._lock:
            return {"history":self._db.execute("SELECT COUNT(*) FROM history WHERE scope=? AND created_at>=?",(scope,time.time()-self.history_days*86400)).fetchone()[0],
                    "memories":self._db.execute("SELECT COUNT(*) FROM memories WHERE scope=?",(scope,)).fetchone()[0]}

    def browse_scope(self, scope, *, kind="history", user=None, before=None, limit=100):
        """Local-admin conversation browser only; never called by model retrieval.

        Bound every read to one exact scope. Mutations keep their existing
        per-record owner/subject checks. Pagination uses immutable integer IDs.
        """
        self._key(scope)
        if user: self._key(user)
        if kind not in {"history","memories"} or not 1<=limit<=200:
            raise ValueError("Invalid conversation browse query")
        clauses=["scope=?"]; params=[scope]
        if user:
            clauses.append("user_id=?" if kind=="history" else "(subject=? OR subject IS NULL)")
            params.append(user)
        if kind=="history":
            clauses.append("created_at>=?"); params.append(time.time()-self.history_days*86400)
        if before is not None:
            clauses.append("id<?"); params.append(before)
        with self._lock:
            rows=self._db.execute("SELECT * FROM "+kind+" WHERE "+" AND ".join(clauses)+" ORDER BY id DESC LIMIT ?",params+[limit+1]).fetchall()
            more=len(rows)>limit; rows=rows[:limit]
            result=[self._memory_record(row) if kind=="memories" else dict(row) for row in rows]
        return {"items":list(reversed(result)),"next_before":rows[-1]["id"] if more else None,"scope":scope,"kind":kind}

    def list_history(self, scope: str, actor: str, *, subject: str | None = None, is_owner: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        """Administrative history view; non-owners may inspect only themselves."""
        subject = subject or actor
        if subject != actor and not is_owner:
            raise PermissionError("Cannot inspect another member's history")
        return self.recent_history(scope, subject, limit)

    def _memory_record(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["source_ids"] = [r[0] for r in self._db.execute("SELECT history_id FROM memory_sources WHERE memory_id=? ORDER BY history_id", (row["id"],)).fetchall()]
        facet = self._db.execute("SELECT facet FROM memory_facets WHERE memory_id=?", (row["id"],)).fetchone()
        result["facet"] = facet[0] if facet else "general"
        from .member_notes import metadata
        result.update(metadata(self,row["id"]))
        return result

    def add_memory(self, scope: str, actor: str, text: str, kind: str = "personal", subject: str | None = None,
                   is_owner: bool = False, source: str = "explicit", source_ids: list[int] | None = None) -> int:
        """Save a personal fact or owner-authored group/event/relation memory.

        An inferred memory is always personal, is stored only with consent, and
        must cite existing history IDs belonging to that same scoped member.
        Public memories are explicit owner actions, never model-extracted facts.
        """
        self._key(scope); self._key(actor)
        text = self._text(text)
        kind = "group" if kind == "public" else kind
        if kind not in ("personal", "group", "event", "relation"):
            raise ValueError("Unsupported memory kind")
        if source not in ("explicit", "inferred"):
            raise ValueError("Source must be explicit or inferred")
        if kind == "personal":
            subject = self._key(subject or actor)
            if subject != actor and not is_owner:
                raise PermissionError("Cannot save another member's personal memory")
        else:
            if not is_owner:
                raise PermissionError("Only the owner may edit shared memories")
            if subject is not None or source != "explicit":
                raise ValueError("Shared memories must be explicit and have no personal subject")
        refs = list(dict.fromkeys(source_ids or []))
        if len(refs) > 500 or any(not isinstance(x, int) or isinstance(x, bool) or x < 1 for x in refs):
            raise ValueError("Invalid source history IDs")
        if refs and kind != "personal":
            raise ValueError("Public memories cannot link private history")
        if source == "inferred" and (not refs or subject != actor):
            raise ValueError("Inferred summaries require the member's own source history")
        with self._lock, self._db:
            if source == "inferred" and not self._consent(scope, actor):
                raise PermissionError("Automatic memory requires consent")
            for ref in refs:
                row = self._db.execute("SELECT scope,user_id,role FROM history WHERE id=?", (ref,)).fetchone()
                if row is None or row["scope"] != scope or row["user_id"] != subject:
                    raise ValueError("Source history is unavailable or belongs to another member")
                if source == "inferred" and row["role"] != "user":
                    raise ValueError("Assistant output is not evidence for a personal fact")
            existing = self._db.execute("SELECT id FROM memories WHERE scope=? AND subject IS ? AND kind=? AND text=? AND source=?",
                                        (scope, subject, kind, text, source)).fetchone()
            if existing:
                memory_id = existing[0]
            else:
                now = time.time()
                memory_id = self._db.execute("INSERT INTO memories(scope,subject,actor,kind,text,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                             (scope, subject, actor, kind, text, source, now, now)).lastrowid
                self._audit("memory_add", scope, actor, memory_id)
            for ref in refs:
                self._db.execute("INSERT OR IGNORE INTO memory_sources VALUES (?,?)", (memory_id, ref))
            return memory_id

    def list_memories(self, scope: str, actor: str, is_owner: bool = False, subject: str | None = None) -> list[dict[str, Any]]:
        """Owner may inspect all scoped records; ordinary members see own + public.

        Setting ``subject`` filters to that member's personal records only. This
        administrative API is deliberately separate from ``retrieve``, which
        never exposes another member's personal facts, including to the owner.
        """
        self._key(scope); self._key(actor)
        if subject is not None:
            self._key(subject)
            if subject != actor and not is_owner:
                raise PermissionError("Cannot inspect another member's memory")
        with self._lock:
            if subject is not None:
                rows = self._db.execute("SELECT * FROM memories WHERE scope=? AND subject=? ORDER BY id", (scope, subject)).fetchall()
            elif is_owner:
                rows = self._db.execute("SELECT * FROM memories WHERE scope=? ORDER BY id", (scope,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM memories WHERE scope=? AND (subject=? OR subject IS NULL) ORDER BY id", (scope, actor)).fetchall()
            return [self._memory_record(row) for row in rows]

    def _editable_memory(self, memory_id: int, scope: str, actor: str, is_owner: bool) -> sqlite3.Row:
        self._key(scope); self._key(actor)
        row = self._db.execute("SELECT * FROM memories WHERE id=? AND scope=?", (memory_id, scope)).fetchone()
        # Same error for missing IDs and IDs outside the caller's scope.
        if row is None or (not is_owner and (row["kind"] != "personal" or row["subject"] != actor)):
            raise PermissionError("Memory not found or access denied")
        return row

    def _erase_sources(self, ids: list[int]) -> None:
        # Erase every derived summary that relied on any deleted source, before
        # removing raw history. No text tombstones are retained.
        for source_id in ids:
            source = self._db.execute("SELECT scope,user_id FROM history WHERE id=?", (source_id,)).fetchone()
            if source:
                self._invalidate_summary(source["scope"], source["user_id"])
            self._erase_paired_reply(source_id)
            self._db.execute("DELETE FROM memories WHERE source='inferred' AND id IN (SELECT memory_id FROM memory_sources WHERE history_id=?)", (source_id,))
            self._db.execute("DELETE FROM history WHERE id=?", (source_id,))
            self._db.execute("DELETE FROM memory_sources WHERE history_id=?", (source_id,))

    def _erase_paired_reply(self, history_id: int) -> None:
        # A model reply can repeat a fact from its source user message. Remove
        # the pair as well, otherwise forgotten content can re-enter context.
        row = self._db.execute("SELECT scope,user_id,role,event_id FROM history WHERE id=?", (history_id,)).fetchone()
        if row and row["role"] == "user":
            self._db.execute("""DELETE FROM memories WHERE source='inferred' AND id IN
                (SELECT memory_id FROM memory_sources WHERE history_id IN
                 (SELECT id FROM history WHERE scope=? AND user_id=? AND role='assistant' AND event_id IN (?,?)))""",
                (row['scope'],row['user_id'],row['event_id'],row['event_id']+':reply'))
            self._db.execute("DELETE FROM history WHERE scope=? AND user_id=? AND role='assistant' AND event_id IN (?,?)",
                             (row["scope"], row["user_id"], row["event_id"], row["event_id"] + ":reply"))

    def update_memory(self, id: int, scope: str, actor: str, text: str, is_owner: bool = False) -> None:
        """Correct a fact and erase old raw sources/derived copies of that fact."""
        text = self._text(text)
        with self._lock, self._db:
            self._editable_memory(id, scope, actor, is_owner)
            from .member_notes import metadata, _text as note_text
            note=metadata(self,id)
            if note:
                text=note_text(text,700 if note['note_type']=='background' else 400)
                self._db.execute('UPDATE member_context_notes SET version=version+1 WHERE memory_id=?',(id,))
            self.trajectories.erase_previews(scope=scope)
            refs = [r[0] for r in self._db.execute("SELECT history_id FROM memory_sources WHERE memory_id=?", (id,)).fetchall()]
            # Make the target explicit first so invalidating old inferred facts
            # preserves this corrected record and its stable ID.
            self._db.execute("UPDATE memories SET text=?,source='explicit',updated_at=? WHERE id=?", (text, time.time(), id))
            # A corrected sentence need not have the old extracted meaning.
            self._db.execute("DELETE FROM memory_facets WHERE memory_id=?", (id,))
            self._erase_sources(refs)
            self._audit("memory_correct", scope, actor, id)

    def delete_memory(self, id: int, scope: str, actor: str, is_owner: bool = False) -> None:
        """Erase the record, its raw sources, and all summaries using those sources."""
        with self._lock, self._db:
            self._editable_memory(id, scope, actor, is_owner)
            self.trajectories.erase_previews(scope=scope)
            refs = [r[0] for r in self._db.execute("SELECT history_id FROM memory_sources WHERE memory_id=?", (id,)).fetchall()]
            self._erase_sources(refs)
            self._db.execute("DELETE FROM memories WHERE id=?", (id,))
            self._audit("memory_delete", scope, actor, id)

    def _editable_history(self, id: int, scope: str, actor: str, is_owner: bool) -> sqlite3.Row:
        self._key(scope); self._key(actor)
        row = self._db.execute("SELECT * FROM history WHERE id=? AND scope=?", (id, scope)).fetchone()
        if row is None or (row["user_id"] != actor and not is_owner):
            raise PermissionError("History not found or access denied")
        return row

    def delete_history(self, id: int, scope: str, actor: str, is_owner: bool = False) -> None:
        with self._lock, self._db:
            row = self._editable_history(id, scope, actor, is_owner)
            self.trajectories.erase_previews(scope=scope)
            self._invalidate_summary(scope, row["user_id"], rewind_to=id - 1)
            self._erase_sources([id])
            self._audit("history_delete", scope, actor, id)

    def update_history(self, id: int, scope: str, actor: str, text: str, is_owner: bool = False) -> None:
        """Correct raw history and invalidate summaries based on the old text."""
        text = self._text(text, maximum=32000)
        with self._lock, self._db:
            row = self._editable_history(id, scope, actor, is_owner)
            self.trajectories.erase_previews(scope=scope)
            self._invalidate_summary(scope, row["user_id"], rewind_to=id - 1)
            self._erase_paired_reply(id)
            self._db.execute("DELETE FROM memories WHERE source='inferred' AND id IN (SELECT memory_id FROM memory_sources WHERE history_id=?)", (id,))
            self._db.execute("UPDATE history SET text=? WHERE id=?", (text, id))
            self._audit("history_correct", scope, actor, id)

    def forget_me(self, scope: str, user: str) -> dict[str, int]:
        """Erase all of this member's personal data in this scope and stop collection.

        Shared group rules/events remain: they are owner-curated public records.
        Erasure from previously exported backups requires deleting those backups.
        """
        self._key(scope); self._key(user)
        with self._lock, self._db:
            self._invalidate_summary(scope, user)
            self.trajectories.erase_previews(scope=scope)
            memories = self._db.execute("DELETE FROM memories WHERE scope=? AND subject=?", (scope, user)).rowcount
            history = self._db.execute("DELETE FROM history WHERE scope=? AND user_id=?", (scope, user)).rowcount
            self._db.execute("DELETE FROM trajectories WHERE scope=? AND user_id=?", (scope, user))
            self._db.execute("INSERT INTO consent VALUES (?,?,0,?) ON CONFLICT(scope,user_id) DO UPDATE SET enabled=0,updated_at=excluded.updated_at", (scope, user, time.time()))
            self._audit("forget_member", scope, user)
            return {"memories": memories, "history": history}

    def summary_candidates(self, scope: str, user: str, limit: int = 40) -> list[dict[str, Any]]:
        """Return unsummarized *user* statements; assistant output is not evidence."""
        self._key(scope); self._key(user); self._limit(limit)
        with self._lock, self._db:
            if not self._consent(scope, user):
                return []
            self._prune(scope, user)
            rows = self._db.execute("SELECT * FROM history h WHERE scope=? AND user_id=? AND role='user' AND NOT EXISTS (SELECT 1 FROM memory_sources ms WHERE ms.history_id=h.id) ORDER BY id LIMIT ?", (scope, user, limit)).fetchall()
            return [dict(r) for r in rows if not hidden_message(r["text"])]

    def _ensure_summary(self, scope: str, user: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO summary_progress(scope,user_id) VALUES (?,?)", (scope, user))

    def _invalidate_summary(self, scope: str, user: str, rewind_to: int | None = None) -> None:
        """Cancel a claimed extraction before its evidence can become stale."""
        self._ensure_summary(scope, user)
        self._db.execute("UPDATE summary_progress SET generation=generation+1,claim_id='',last_status='changed',retry_at=0 WHERE scope=? AND user_id=?", (scope, user))
        if rewind_to is not None:
            self._db.execute("UPDATE summary_progress SET through_id=MIN(through_id,?) WHERE scope=? AND user_id=?", (max(0, rewind_to), scope, user))

    def import_summary_checkpoint(self, scope: str, user: str, through: int) -> None:
        """One-time additive migration of the legacy default-profile checkpoint."""
        self._key(scope); self._key(user)
        if not isinstance(through, int) or isinstance(through, bool) or through < 0:
            raise ValueError("Invalid summary checkpoint")
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO summary_progress(scope,user_id,through_id) VALUES (?,?,?)", (scope, user, through))

    def begin_summary(self, scope: str, user: str, *, threshold: int = 12,
                      force: bool = False, limit: int = 100, max_wait_seconds: int | None = None,
                      revisit: bool = False) -> dict[str, Any]:
        """Claim oldest retained unprocessed statements; never depend on replies.

        A claim expires after five minutes so a crashed process cannot leave a
        member permanently stuck. ``force`` bypasses only the size threshold;
        consent, existing claims and retry backoff still apply.
        """
        self._key(scope); self._key(user); self._limit(threshold, 100); self._limit(limit, 100)
        if not isinstance(force, bool) or not isinstance(revisit,bool):
            raise ValueError("force must be boolean")
        if max_wait_seconds is not None:
            self._limit(max_wait_seconds,86400)
        with self._lock, self._db:
            if not self._consent(scope, user):
                return {"status": "disabled"}
            self._prune(scope, user)
            self._ensure_summary(scope, user)
            state = self._db.execute("SELECT * FROM summary_progress WHERE scope=? AND user_id=?", (scope, user)).fetchone()
            now = time.time()
            if state["retry_at"] > now:
                return {"status": "retry_wait", "retry_after_seconds": max(1, math.ceil(state["retry_at"] - now))}
            rows = self._db.execute("SELECT * FROM history WHERE scope=? AND user_id=? AND role='user' AND id>? ORDER BY id LIMIT ?", (scope, user, 0 if revisit else state["through_id"], limit)).fetchall()
            if not rows:
                return {"status": "empty"}
            # Also respect the marker in history retained from older versions.
            through = rows[-1]["id"]
            rows = [row for row in rows if not hidden_message(row["text"])]
            if not rows:
                if not revisit:
                    self._db.execute("UPDATE summary_progress SET through_id=MAX(through_id,?) WHERE scope=? AND user_id=?", (through,scope,user))
                return {"status": "empty"}
            overdue = max_wait_seconds is not None and now-min(row["created_at"] for row in rows)>=max_wait_seconds
            if not force and len(rows) < threshold and not overdue:
                return {"status": "below_threshold", "pending": len(rows), "remaining": threshold - len(rows)}
            generation=state['generation']
            if revisit:
                # Revisit only retained, consented history. Keep existing facts;
                # remaining batches use the usual background queue and budget.
                generation+=1
                self._db.execute("UPDATE summary_progress SET through_id=0,generation=? WHERE scope=? AND user_id=?",(generation,scope,user))
                self._audit('summary_revisit',scope,user)
            claim_id = uuid.uuid4().hex
            self._db.execute("UPDATE summary_progress SET claim_id=?,last_status='running',last_attempt_at=?,retry_at=? WHERE scope=? AND user_id=?", (claim_id, now, now + 300, scope, user))
            return {"status": "ready", "batch": {"claim_id": claim_id, "generation": generation, "history": [dict(r) for r in rows]}}

    def _summary_current(self, scope: str, user: str, batch: dict) -> bool:
        state = self._db.execute("SELECT generation,claim_id FROM summary_progress WHERE scope=? AND user_id=?", (scope, user)).fetchone()
        return bool(state and state["claim_id"] and state["claim_id"] == batch.get("claim_id")
                    and state["generation"] == batch.get("generation") and self._consent(scope, user))

    def finish_summary(self, scope: str, user: str, batch: dict, items: list[dict], *, background=None, interactions=None) -> dict[str, Any]:
        """Atomically validate evidence, save valid facts and advance the cursor.

        Checking source contents as well as IDs prevents an edit made during a
        cloud request from reintroducing an old inference. New incoming messages
        do not invalidate the claim and remain pending for the next batch.
        """
        self._key(scope); self._key(user)
        if not isinstance(batch, dict) or not isinstance(items, list):
            raise ValueError("Invalid summary result")
        with self._lock, self._db:
            if not self._summary_current(scope, user, batch):
                return {"status": "superseded", "saved": 0}
            evidence = batch.get("history", [])
            if not evidence:
                raise ValueError("Missing summary evidence")
            refs = {row["id"]: row for row in evidence}
            for number, source in refs.items():
                row = self._db.execute("SELECT * FROM history WHERE id=?", (number,)).fetchone()
                if (not row or row["scope"] != scope or row["user_id"] != user or row["role"] not in {"user","assistant"}
                        or row["text"] != source["text"] or hidden_message(row["text"])):
                    self._invalidate_summary(scope, user)
                    return {"status": "superseded", "saved": 0}
            saved = 0
            accepted = 0
            saved_ids = []
            now = time.time()
            for item in items[:6]:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
                    continue
                source_ids = item.get("source_ids")
                if (not isinstance(source_ids, list) or not source_ids or len(source_ids) > 100
                        or any(not isinstance(x, int) or isinstance(x, bool) or x not in refs or refs[x]['role']!='user' for x in source_ids)):
                    continue
                text = item["text"].strip()[:500]
                accepted += 1
                existing = self._db.execute("SELECT id FROM memories WHERE scope=? AND subject=? AND kind='personal' AND text=? AND source='inferred'", (scope, user, text)).fetchone()
                if existing:
                    memory_id = existing[0]
                else:
                    memory_id = self._db.execute("INSERT INTO memories(scope,subject,actor,kind,text,source,created_at,updated_at) VALUES (?,?,?,'personal',?,'inferred',?,?)", (scope, user, user, text, now, now)).lastrowid
                    self._audit("memory_add", scope, user, memory_id)
                    saved += 1
                    saved_ids.append(memory_id)
                for source_id in set(source_ids):
                    self._db.execute("INSERT OR IGNORE INTO memory_sources VALUES (?,?)", (memory_id, source_id))
                facet = item.get("facet", "general")
                if not isinstance(facet,str) or facet not in {"identity","preference","experience","ongoing","general"}:
                    facet = "general"
                self._db.execute("INSERT OR REPLACE INTO memory_facets VALUES (?,?)", (memory_id,facet))
            if items and not accepted:
                # A malformed non-empty model result is not evidence that the
                # conversation contained no facts. Keep it pending for retry.
                raise ValueError("summary_invalid")
            from .member_notes import apply_summary
            note_ids=apply_summary(self,scope,user,batch,refs,intro=background,interactions=interactions)
            saved+=len(note_ids)
            saved_ids.extend(note_ids)
            status = "saved" if saved else "no_facts"
            user_ids=[n for n,r in refs.items() if r['role']=='user']
            if not user_ids:raise ValueError('summary_invalid')
            self._db.execute("UPDATE summary_progress SET through_id=MAX(through_id,?),claim_id='',last_status=?,last_success_at=?,last_saved=?,retry_at=0 WHERE scope=? AND user_id=?", (max(user_ids), status, now, saved, scope, user))
            result = {"status": status, "saved": saved, "processed": len(user_ids)}
            if saved_ids:
                result["memory_ids"] = saved_ids
            return result

    def fail_summary(self, scope: str, user: str, batch: dict, code: str, retry_seconds: int = 60) -> dict[str, Any]:
        """Store an allowlisted status, never a model error body or prompt."""
        self._key(scope); self._key(user)
        allowed = {"cloud_budget", "cloud_backoff", "provider_unavailable", "invalid_summary_object", "summary_invalid", "missing_api_key", "unauthorized", "rate_limited", "timeout", "upstream_error", "network_error", "invalid_reply_object", "invalid_json", "provider_failure"}
        safe = code if isinstance(code, str) and code in allowed else "provider_failure"
        retry = min(900, max(10, retry_seconds)) if isinstance(retry_seconds, int) else 60
        with self._lock, self._db:
            if not self._summary_current(scope, user, batch):
                return {"status": "superseded", "saved": 0}
            self._db.execute("UPDATE summary_progress SET claim_id='',last_status=?,retry_at=? WHERE scope=? AND user_id=?", (safe, time.time() + retry, scope, user))
            return {"status": safe, "saved": 0, "retry_after_seconds": retry}

    def collection_status(self, auto_summary_every: int = 12, max_wait_minutes: int = 30, *, scope_filter: str | None = None) -> dict[str, Any]:
        """Inspect collection and extraction progress without including content."""
        self._limit(auto_summary_every, 100)
        self._limit(max_wait_minutes,1440)
        with self._lock, self._db:
            stats = self.stats()
            keys = self._db.execute("SELECT * FROM (SELECT scope,user_id FROM consent UNION SELECT scope,user_id FROM history UNION SELECT scope,subject AS user_id FROM memories WHERE subject IS NOT NULL)" + (" WHERE scope=?" if scope_filter else "") + " ORDER BY scope,user_id",(scope_filter,) if scope_filter else ()).fetchall()
            members = []
            now = time.time()
            for key in keys[:500]:
                scope, user = key["scope"], key["user_id"]
                policy=self.summary_policy(scope)
                every=policy.get('auto_summary_every',auto_summary_every)
                wait=policy.get('auto_summary_max_wait_minutes',max_wait_minutes)
                state_row = self._db.execute("SELECT * FROM summary_progress WHERE scope=? AND user_id=?", (scope, user)).fetchone()
                state = dict(state_row) if state_row else {}
                counts = self._db.execute("SELECT COUNT(*),COALESCE(SUM(role='user'),0),MAX(CASE WHEN role='user' THEN created_at END),COALESCE(SUM(role='user' AND id>?),0) FROM history WHERE scope=? AND user_id=?", (state.get("through_id", 0), scope, user)).fetchone()
                facts = self._db.execute("SELECT COUNT(*),COALESCE(SUM(source='explicit'),0),COALESCE(SUM(source='inferred'),0) FROM memories WHERE scope=? AND subject=?", (scope, user)).fetchone()
                oldest = self._db.execute("SELECT MIN(created_at) FROM history WHERE scope=? AND user_id=? AND role='user' AND id>?",
                    (scope,user,state.get("through_id",0))).fetchone()[0]
                members.append({"scope": scope, "user": user, "enabled": self._consent(scope, user),
                                "collection_source": self.collection_source(scope, user),
                                "environment": "demo" if scope.startswith("demo:") else "qq",
                                "history": counts[0], "user_messages": counts[1], "memories": facts[0],
                                "explicit": facts[1], "inferred": facts[2], "pending": counts[3],
                                "last_observed_at": counts[2], "remaining_to_summary": max(0, every - counts[3]),
                                "oldest_pending_at":oldest,
                                "summary_wait_seconds":max(0,math.ceil(oldest+wait*60-now)) if oldest is not None else None,
                                "summary": {"status": state.get("last_status", "not_started"),
                                            "last_attempt_at": state.get("last_attempt_at"), "last_success_at": state.get("last_success_at"),
                                            "last_saved": state.get("last_saved", 0),
                                            "retry_after_seconds": max(0, math.ceil(state.get("retry_at", 0) - now))}})
            group_policies = [dict(row) | {"enabled": bool(row["enabled"])} for row in self._db.execute("SELECT scope,enabled,updated_at FROM group_collection" + (" WHERE scope=?" if scope_filter else "") + " ORDER BY scope",(scope_filter,) if scope_filter else ())]
            if scope_filter:
                stats=self.scope_stats(scope_filter) | {"consenting_members":sum(self._consent(row["scope"],row["user_id"]) for row in keys)}
            return {"stats": stats, "auto_summary_every": auto_summary_every,"auto_summary_max_wait_minutes":max_wait_minutes,
                    "retention": {"history_days": self.history_days, "max_history": self.max_history},
                    "members": members, "member_count": len(keys), "truncated": len(keys) > 500,
                    "group_policies": group_policies, "enabled_groups": sum(row["enabled"] for row in group_policies),
                    "collection": "group_default_with_member_overrides", "retrieval": "scoped_lexical"}

    def due_summaries(self, threshold: int = 12, limit: int = 20, *,
                      allowed_scopes: list[str] | None = None, max_wait_seconds: int | None = None) -> list[dict[str, Any]]:
        """Fair queue of effectively enabled members; no conversation content."""
        self._limit(threshold, 100); self._limit(limit, 100)
        if max_wait_seconds is not None:
            self._limit(max_wait_seconds,86400)
        scope_clause = ""
        scope_values = []
        if allowed_scopes is not None:
            if not isinstance(allowed_scopes, list) or len(allowed_scopes) > 10000:
                raise ValueError("Invalid allowed scopes")
            scope_values = list(dict.fromkeys(self._key(scope) for scope in allowed_scopes))
            if not scope_values:
                return []
            scope_clause = " AND h.scope IN (" + ",".join("?" for _ in scope_values) + ")"
        with self._lock, self._db:
            now = time.time()
            self._db.execute("DELETE FROM history WHERE created_at<?", (now - self.history_days * 86400,))
            rows = self._db.execute("""
                SELECT h.scope,h.user_id AS user,COUNT(h.id) AS pending
                FROM history h
                LEFT JOIN consent c ON c.scope=h.scope AND c.user_id=h.user_id
                LEFT JOIN group_collection g ON g.scope=h.scope
                LEFT JOIN summary_progress p ON p.scope=h.scope AND p.user_id=h.user_id
                LEFT JOIN memory_policies mp ON mp.scope=h.scope
                WHERE h.role='user' AND h.id>COALESCE(p.through_id,0)
                    AND COALESCE(c.enabled,g.enabled,0)=1 AND COALESCE(p.retry_at,0)<=?
            """ + scope_clause + """
                GROUP BY h.scope,h.user_id
                HAVING COUNT(h.id)>=COALESCE(mp.auto_summary_every,?) OR (COALESCE(mp.auto_summary_max_wait_minutes*60,?) IS NOT NULL AND MIN(h.created_at)<=?-COALESCE(mp.auto_summary_max_wait_minutes*60,?))
                ORDER BY COALESCE(p.last_attempt_at,0),MIN(h.id)
                LIMIT ?
            """, (now, *scope_values, threshold,max_wait_seconds,now,max_wait_seconds, limit)).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _tokens(text: str) -> set[str]:
        result = set(re.findall(r"[a-z0-9_]+", text.lower()))
        for run in re.findall(r"[\u3400-\u9fff]+", text):
            result.update(run[i:i+2] for i in range(max(0, len(run)-1)))
            # Single-character queries remain usable without flooding normal
            # multi-character Chinese queries with low-information unigrams.
            if len(run) == 1:
                result.add(run)
        return result

    def retrieve(self, scope: str, user: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Lexical retrieval: own personal memories + shared records in this scope.

        No administrator bypass exists here. Query content can never widen the
        scope. Chinese bigrams avoid a local embedding model and vector service.
        """
        self._key(scope); self._key(user); self._limit(limit, 32)
        if not isinstance(query, str) or len(query) > 32000:
            raise ValueError("Invalid retrieval query")
        terms = self._tokens(query)
        # An emoji/punctuation-only query is not an instruction to disclose a
        # general profile. Keep that policy separate from relevant retrieval.
        if not terms:
            return []
        rows = self.list_memories(scope, user)
        # For an explicitly queried single Han character, literal occurrence
        # within a longer run is still a match (猫 -> 我养了一只猫). Only add
        # those queried characters; normal multi-character queries keep their
        # bigrams instead of matching on every low-information character.
        single_han = {term for term in terms if re.fullmatch(r"[\u3400-\u9fff]", term)}
        documents = [self._tokens(row["text"]) | {term for term in single_han if term in row["text"]}
                     for row in rows]
        counts = {term: sum(term in doc for doc in documents) for term in terms}
        ranked = []
        for row, tokens in zip(rows, documents):
            matches = terms & tokens
            score = sum(math.log(1 + (len(rows) + 1) / (counts[term] + 1)) for term in matches)
            if not matches:
                continue
            ranked.append({**row, "score": round(score, 6)})
        ranked.sort(key=lambda row: (row["score"], row["updated_at"], row["id"]), reverse=True)
        return ranked[:limit]

    def stats(self) -> dict[str, int]:
        """Content-free counts, suitable for a local health/management view."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM history WHERE created_at<?", (time.time() - self.history_days * 86400,))
            policy_counts = self._db.execute("""
                WITH known AS (
                    SELECT scope,user_id FROM consent UNION SELECT scope,user_id FROM history
                    UNION SELECT scope,subject AS user_id FROM memories WHERE subject IS NOT NULL
                ) SELECT COALESCE(SUM(COALESCE(c.enabled,g.enabled,0)=1),0),
                    COALESCE(SUM(c.enabled=1),0),
                    COALESCE(SUM(c.enabled IS NULL AND g.enabled=1),0),
                    COALESCE(SUM(c.enabled=0),0)
                FROM known k LEFT JOIN consent c ON c.scope=k.scope AND c.user_id=k.user_id
                LEFT JOIN group_collection g ON g.scope=k.scope
            """).fetchone()
            return {
                "memories": self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                "history": self._db.execute("SELECT COUNT(*) FROM history").fetchone()[0],
                # Keep the legacy field for callers, but expose the actual
                # reasons separately: a group default is not personal consent.
                "consenting_members": policy_counts[0],
                "explicit_enabled_members": policy_counts[1],
                "group_inherited_members": policy_counts[2],
                "excluded_members": policy_counts[3],
                "enabled_groups": self._db.execute("SELECT COUNT(*) FROM group_collection WHERE enabled=1").fetchone()[0],
                "scopes": self._db.execute("SELECT COUNT(DISTINCT scope) FROM (SELECT scope FROM memories UNION SELECT scope FROM history)").fetchone()[0],
            }

    def backup(self, path: str | Path) -> None:
        """Create a consistent SQLite snapshot using the SQLite backup API."""
        destination = Path(path)
        if self.path != ":memory:" and destination.resolve() == Path(self.path).resolve():
            raise ValueError("Backup destination must differ from the live database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Never overwrite a previous backup silently.
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with self._lock:
                target = sqlite3.connect(destination)
                try:
                    self._db.backup(target)
                finally:
                    target.close()
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

