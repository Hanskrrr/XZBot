"""Durable, source-scoped pauses shared by previews, creation and polling.

Only source IDs, fixed error codes and timestamps are stored. A short lease
prevents concurrent requests from multiple service objects. No credentials,
response body or platform fingerprint is retained or synthesized.
"""
from __future__ import annotations

from contextlib import closing
import math
import os
from pathlib import Path
import re
import sqlite3
import time

HEALTH_LIMIT = 1000
HEALTH_RETENTION_SECONDS = 30 * 86400
HEALTH_FIELDS = ("last_success_at", "last_failure_at", "last_failure_code", "failures_total")


def _safe_failure_code(value):
    """Only adapter-owned codes may enter diagnostic history."""
    for prefix in ("weibo", "bilibili_dynamic"):
        if value in {prefix + "_" + suffix for suffix in (
                "access_rejected", "rate_limited", "login_required", "auth_or_api_rejected",
                "response_invalid", "response_too_large", "response_not_json", "content_unavailable",
                "network_error", "network_or_format")}:
            return value
        if re.fullmatch(prefix + r"_http_[1-5][0-9]{2}", value):
            return value
    return "subscription_source_error"


class SourceCooldown:
    def __init__(self, root: Path, *, clock=time.time):
        self.path = Path(root) / "data/bilibili.sqlite3"
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            except FileExistsError:
                pass
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS subscription_source_cooldowns (
                source TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL, last_error TEXT NOT NULL DEFAULT ''
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS subscription_source_health (
                source TEXT PRIMARY KEY, last_success_at REAL NOT NULL DEFAULT 0,
                last_failure_at REAL NOT NULL DEFAULT 0, last_failure_code TEXT NOT NULL DEFAULT '',
                failures_total INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
            )""")
            self._prune_health(db, self.clock())

    @staticmethod
    def _prune_health(db, now):
        db.execute("DELETE FROM subscription_source_health WHERE updated_at<?", (now-HEALTH_RETENTION_SECONDS,))
        db.execute("""DELETE FROM subscription_source_health WHERE source IN (
            SELECT source FROM subscription_source_health ORDER BY updated_at DESC,source DESC
            LIMIT -1 OFFSET ?)""", (HEALTH_LIMIT,))

    def _record_health(self, db, source, now, *, failure=""):
        self._prune_health(db, now)
        if not db.execute("SELECT 1 FROM subscription_source_health WHERE source=?", (source,)).fetchone():
            if db.execute("SELECT COUNT(*) FROM subscription_source_health").fetchone()[0] >= HEALTH_LIMIT:
                db.execute("""DELETE FROM subscription_source_health WHERE source=(
                    SELECT source FROM subscription_source_health ORDER BY updated_at,source LIMIT 1)""")
        if failure:
            db.execute("""INSERT INTO subscription_source_health
                (source,last_failure_at,last_failure_code,failures_total,updated_at) VALUES (?,?,?,1,?)
                ON CONFLICT(source) DO UPDATE SET last_failure_at=excluded.last_failure_at,
                last_failure_code=excluded.last_failure_code,failures_total=MIN(failures_total+1,2147483647),
                updated_at=excluded.updated_at""", (source, now, failure, now))
        else:
            db.execute("""INSERT INTO subscription_source_health(source,last_success_at,updated_at)
                VALUES (?,?,?) ON CONFLICT(source) DO UPDATE SET last_success_at=excluded.last_success_at,
                updated_at=excluded.updated_at""", (source, now, now))

    def health_many(self, sources):
        """Read only requested metadata; unrelated source IDs never leave here."""
        sources = list(dict.fromkeys(sources))[:HEALTH_LIMIT]
        result = {source: dict(zip(HEALTH_FIELDS, (0, 0, "", 0))) for source in sources}
        if not sources:
            return result
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            placeholders = ",".join("?" for _ in sources)
            rows = db.execute(f"""SELECT source,last_success_at,last_failure_at,last_failure_code,failures_total
                FROM subscription_source_health WHERE updated_at>=? AND source IN ({placeholders})""",
                (self.clock()-HEALTH_RETENTION_SECONDS, *sources))
            for row in rows:
                result[row[0]] = dict(zip(HEALTH_FIELDS, row[1:]))
        return result

    def claim(self, source):
        from .subscription_sources import SubscriptionError
        prefix = "weibo" if source.startswith("weibo:") else "bilibili_dynamic"
        now = self.clock()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT retry_at,lease_until FROM subscription_source_cooldowns WHERE source=?", (source,)).fetchone()
            if row and max(row) > now:
                deadline = max(row)
                code = prefix + ("_cooldown" if row[0] > now else "_request_in_progress")
                raise SubscriptionError(code, retry_at=deadline, retry_after_seconds=math.ceil(deadline-now))
            db.execute("DELETE FROM subscription_source_cooldowns WHERE updated_at<? AND retry_at<? AND lease_until<?",
                       (now-7*86400, now, now))
            if row is None and db.execute("SELECT COUNT(*) FROM subscription_source_cooldowns").fetchone()[0] >= 1000:
                raise SubscriptionError("subscription_request_capacity")
            db.execute("""INSERT INTO subscription_source_cooldowns(source,lease_until,updated_at)
                VALUES (?,?,?) ON CONFLICT(source) DO UPDATE SET lease_until=excluded.lease_until,updated_at=excluded.updated_at""",
                       # Weibo can make two sequential, twenty-second bounded
                       # reads. Keep their whole operation under one lease.
                       (source, now+60, now))

    def success(self, source):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM subscription_source_cooldowns WHERE source=?", (source,))
            self._record_health(db, source, self.clock())

    def release(self, source):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("UPDATE subscription_source_cooldowns SET lease_until=0 WHERE source=?", (source,))

    def failed(self, source, error):
        from .subscription_sources import SubscriptionError
        code = _safe_failure_code(str(error))
        now = self.clock()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT failures FROM subscription_source_cooldowns WHERE source=?", (source,)).fetchone()
            failures = min((row[0] if row else 0)+1, 8)
            restricted = code in {"bilibili_dynamic_access_rejected", "bilibili_dynamic_rate_limited",
                                  "weibo_access_rejected", "weibo_rate_limited", "weibo_login_required",
                                  "weibo_response_not_json"}
            delay = min(3600, 900 * 2**(failures-1)) if restricted else min(300, 30 * 2**(failures-1))
            # Retry-After may be longer than our local exponential backoff.
            # Source clients already bound it, and the persisted deadline is
            # shared by polling, previews and a restarted service.
            delay = max(delay, min(86400, error.retry_after_seconds))
            retry_at = now + delay
            db.execute("""INSERT INTO subscription_source_cooldowns
                (source,failures,retry_at,lease_until,updated_at,last_error) VALUES (?,?,?,0,?,?)
                ON CONFLICT(source) DO UPDATE SET failures=excluded.failures,retry_at=excluded.retry_at,
                lease_until=0,updated_at=excluded.updated_at,last_error=excluded.last_error""",
                       (source, failures, retry_at, now, code))
            self._record_health(db, source, now, failure=code)
        return SubscriptionError(code, retry_at=retry_at, retry_after_seconds=delay)
