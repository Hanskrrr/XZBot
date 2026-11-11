"""Legacy compatibility utility; the Engine no longer imports or uses this store.

Old, content-free addressee bindings shared by managed QQ accounts.

A public group and human member identify the conversation; only the selected
bot, an opaque generation token and expiry are stored. Late send receipts may
extend their own binding but cannot take it back from a newer explicit target.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable
import uuid

from .dialogue import refers_back
from .host import protect, safe_open
from .runtime_layout import runtime_layout
from .settings import QQ_ID_PATTERN


def explicitly_group_addressed(text: str) -> bool:
    """Recognize plural invitations, not a third person's reported '都说'."""
    if not isinstance(text, str):
        return False
    text = text.strip()[:600]
    if re.search(r"你[们俩]|您们|妳[们俩]|两位|二位", text):
        return True
    # '大家都说你很自恋' reports what people said to one addressee; it
    # does not invite every bot to answer. Require a vocative or request verb.
    if re.match(
        r"^(?:(?:请|麻烦|想请|想问问|问问)\s*)?(?:大家|各位)\s*"
        r"(?:[，,:：！!]|(?:请|来|一起|都)?(?:回答|说说|评价|看看|看一下|怎么看|"
        r"觉得(?:呢|怎么样)|有什么(?:想法|看法)|继续|聊聊|帮我|帮忙|投票|选|讲讲|解释|能不能|能否))", text):
        return True
    return bool(re.match(
        r"^(?:(?:请|麻烦|能不能|可以|要不)\s*)?(?:"
        r"一起(?:回答|说说|聊聊)|同时回答|分别回答|各自(?:说|回答)|"
        r"都(?:说说|说一下|回答)|都说(?:[吧呀啊呢，,。！!？?\s]|$))", text))


def is_singular_followup(text: str) -> bool:
    """A narrow local continuation cue, never proof of semantic intent."""
    if not isinstance(text, str) or explicitly_group_addressed(text):
        return False
    text = text.strip()
    if re.search(r"[你您妳]", text[:600]):
        return True
    # Generic observations about someone else should not be captured merely
    # because refers_back sees a time word such as 刚才 or a verb like 继续.
    if re.search(r"(?:他|她|它|他们|她们|它们|有人|群友|朋友|新闻).{0,8}(?:说|发|继续|接着|刚才|刚刚)", text):
        return False
    return bool(refers_back(text) and re.match(
        r"^(?:(?:那|那么|嗯|所以|好[的吧]?|然后|请|麻烦|能不能|可以|要不)[，,\s]*)*"
        r"(?:再|重|继续|接着|刚才|刚刚|上一[个条句次轮]|换|这[个句条])", text))


class GroupTargets:
    def __init__(self, root: str | Path, *, clock: Callable[[], float] = time.time,
                 max_targets: int = 4096):
        if type(max_targets) is not int or not 1 <= max_targets <= 4096:
            raise ValueError("Invalid group target capacity")
        self.clock, self.max_targets = clock, max_targets
        self.path = runtime_layout(Path(root)).manager_root / "data/group_targets.sqlite3"
        if self.path.parent.is_symlink():
            raise ValueError("group_targets_directory_invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = safe_open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        protect(self.path)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS group_targets (
                group_id TEXT NOT NULL,user_id TEXT NOT NULL,bot_id TEXT NOT NULL,
                token TEXT NOT NULL,expires_at REAL NOT NULL,claimed_at REAL NOT NULL,
                PRIMARY KEY(group_id,user_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS group_target_expiry ON group_targets(expires_at)")

    @contextmanager
    def _db(self, *, write=True):
        if self.path.is_symlink():
            raise ValueError("group_targets_path_invalid")
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def _ids(*values):
        if not all(isinstance(value, str) and re.fullmatch(QQ_ID_PATTERN, value) for value in values):
            raise ValueError("Invalid group target identity")

    @staticmethod
    def _duration(seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 7200:
            raise ValueError("Invalid group target duration")

    @staticmethod
    def _prune(db, now):
        db.execute("DELETE FROM group_targets WHERE expires_at<=?", (now,))

    def _claim(self, group, user, bot, seconds, *, only_unbound):
        self._ids(group, user, bot)
        self._duration(seconds)
        # Capture before waiting for SQLite. A delayed older explicit claim
        # must not overwrite a newer one merely by acquiring the lock last.
        now = self.clock()
        with self._db() as db:
            self._prune(db, now)
            previous = db.execute("SELECT claimed_at FROM group_targets WHERE group_id=? AND user_id=?",
                                  (group,user)).fetchone()
            if previous and (only_unbound or previous["claimed_at"] > now):
                return None
            if previous is None and db.execute("SELECT COUNT(*) FROM group_targets").fetchone()[0] >= self.max_targets:
                return None
            token = uuid.uuid4().hex
            db.execute("""INSERT INTO group_targets VALUES (?,?,?,?,?,?)
                ON CONFLICT(group_id,user_id) DO UPDATE SET bot_id=excluded.bot_id,
                token=excluded.token,expires_at=excluded.expires_at,claimed_at=excluded.claimed_at""",
                (group,user,bot,token,now+seconds,now))
            return token

    def claim(self, group, user, bot, seconds):
        """Bind explicit intent; return its token, or None at capacity/staleness."""
        return self._claim(group,user,bot,seconds,only_unbound=False)

    def claim_if_unbound(self, group, user, bot, seconds):
        """A confirmed generic reply may establish a target, never steal one."""
        return self._claim(group,user,bot,seconds,only_unbound=True)

    def get(self, group, user):
        self._ids(group,user)
        # Frequent cross-account lookups need no writer lock. Mutations prune
        # expired rows before enforcing capacity; reads simply exclude them.
        with self._db(write=False) as db:
            row = db.execute("""SELECT bot_id,token,expires_at FROM group_targets
                WHERE group_id=? AND user_id=? AND expires_at>?""",
                             (group,user,self.clock())).fetchone()
            return dict(row) if row else None

    def refresh(self, group, user, bot, token, seconds):
        self._ids(group,user,bot)
        self._duration(seconds)
        if not isinstance(token,str) or not token:
            return False
        with self._db() as db:
            now = self.clock()
            self._prune(db,now)
            return bool(db.execute("""UPDATE group_targets SET expires_at=MAX(expires_at,?)
                WHERE group_id=? AND user_id=? AND bot_id=? AND token=?""",
                (now+seconds,group,user,bot,token)).rowcount)

    def release(self, group, user):
        self._ids(group,user)
        with self._db() as db:
            self._prune(db,self.clock())
            db.execute("DELETE FROM group_targets WHERE group_id=? AND user_id=?", (group,user))

    def release_bot(self, bot):
        self._ids(bot)
        with self._db() as db:
            self._prune(db,self.clock())
            db.execute("DELETE FROM group_targets WHERE bot_id=?", (bot,))
