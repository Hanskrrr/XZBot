import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path

from azusa_bot.memory import MemoryStore


SCOPE = "qq:app1:group:g1"
OTHER = "qq:app1:group:g2"
DM = "qq:app1:dm:u1"


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path, max_history=4)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def history(self, user="u1", text="我喜欢打瓦", event="e1"):
        self.store.set_consent(SCOPE, user, True)
        return self.store.add_history(SCOPE, user, "user", text, event)

    def test_history_requires_scoped_opt_in(self):
        self.assertIsNone(self.store.add_history(SCOPE, "u1", "user", "私密内容", "e1"))
        self.store.set_consent(OTHER, "u1", True)
        self.assertFalse(self.store.consent(SCOPE, "u1"))
        self.assertEqual(self.store.stats()["history"], 0)
        self.history()
        self.assertEqual(len(self.store.recent_history(SCOPE, "u1")), 1)
        self.assertEqual(self.store.recent_history(SCOPE, "u2"), [])

    def test_member_group_dm_and_bot_isolation(self):
        self.store.add_memory(SCOPE, "u1", "u1 喜欢打瓦")
        self.store.add_memory(SCOPE, "u2", "u2 喜欢打瓦")
        self.store.add_memory(OTHER, "u1", "另一群喜欢打瓦")
        self.store.add_memory(DM, "u1", "私聊喜欢打瓦")
        self.store.add_memory("qq:app2:group:g1", "u1", "另一机器人喜欢打瓦")
        self.store.add_memory(SCOPE, "owner", "群里喜欢打瓦", kind="group", is_owner=True)
        found = self.store.retrieve(SCOPE, "u1", "喜欢打瓦")
        self.assertEqual({r["text"] for r in found}, {"u1 喜欢打瓦", "群里喜欢打瓦"})
        # Even an owner asking for u1 facts only gets their own private + public.
        self.assertEqual([r["text"] for r in self.store.retrieve(SCOPE, "owner", "u1 喜欢打瓦")], ["群里喜欢打瓦"])

    def test_shared_memory_requires_owner(self):
        for kind in ("group", "public", "event", "relation"):
            with self.assertRaises(PermissionError):
                self.store.add_memory(SCOPE, "u1", "我是主人", kind=kind)
        with self.assertRaises(PermissionError):
            self.store.add_memory(SCOPE, "u1", "u2 的爱好", subject="u2")
        with self.assertRaises(PermissionError):
            self.store.list_memories(SCOPE, "u1", subject="u2")

    def test_mutation_permissions_include_scope(self):
        personal = self.store.add_memory(SCOPE, "u1", "个人事实")
        shared = self.store.add_memory(SCOPE, "owner", "群事件", kind="event", is_owner=True)
        for target in (personal, shared):
            with self.assertRaises(PermissionError):
                self.store.update_memory(target, SCOPE, "u2", "篡改")
            with self.assertRaises(PermissionError):
                self.store.delete_memory(target, OTHER, "owner", is_owner=True)
        self.store.update_memory(personal, SCOPE, "u1", "已纠正")
        self.store.delete_memory(shared, SCOPE, "owner", is_owner=True)

    def test_inferred_sources_cannot_cross_users_or_scopes(self):
        ref = self.history()
        self.store.set_consent(SCOPE, "u2", True)
        with self.assertRaises(ValueError):
            self.store.add_memory(SCOPE, "u2", "泄露 u1", source="inferred", source_ids=[ref])
        self.store.set_consent(OTHER, "u1", True)
        with self.assertRaises(ValueError):
            self.store.add_memory(OTHER, "u1", "跨群", source="inferred", source_ids=[ref])
        with self.assertRaises(ValueError):
            self.store.add_memory(SCOPE, "owner", "推测关系", kind="relation", is_owner=True, source="inferred", source_ids=[ref])
        with self.assertRaises(ValueError):
            self.store.add_memory(SCOPE, "u1", "无来源摘要", source="inferred")
        assistant_ref = self.store.add_history(SCOPE, "u1", "assistant", "模型虚构的身份", "assistant-event")
        with self.assertRaises(ValueError):
            self.store.add_memory(SCOPE, "u1", "把虚构当事实", source="inferred", source_ids=[assistant_ref])

    def test_inactive_history_expires_when_other_member_is_active(self):
        self.history()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE history SET created_at=1")
        self.history(user="u2", text="新内容", event="new-event")
        self.assertEqual(self.store.stats()["history"], 1)
        self.assertEqual(self.store.recent_history(SCOPE, "u1"), [])

    def test_deleting_summary_erases_sources_and_derived_copies(self):
        ref = self.history(text="需要彻底删除的事实")
        self.store.add_history(SCOPE, "u1", "assistant", "回复重复需要彻底删除的事实", "e1:reply")
        first = self.store.add_memory(SCOPE, "u1", "摘要一", source="inferred", source_ids=[ref])
        self.store.add_memory(SCOPE, "u1", "摘要二", source="inferred", source_ids=[ref])
        self.store.delete_memory(first, SCOPE, "u1")
        self.assertEqual(self.store.list_memories(SCOPE, "u1"), [])
        self.assertEqual(self.store.recent_history(SCOPE, "u1"), [])
        with sqlite3.connect(self.path) as db:
            self.assertNotIn("需要彻底删除的事实", str(db.execute("SELECT * FROM audit").fetchall()))

    def test_correct_summary_removes_old_sources_and_preserves_id(self):
        ref = self.history()
        first = self.store.add_memory(SCOPE, "u1", "误解", source="inferred", source_ids=[ref])
        self.store.add_memory(SCOPE, "u1", "另一个误解", source="inferred", source_ids=[ref])
        self.store.update_memory(first, SCOPE, "u1", "纠正后的偏好")
        memories = self.store.list_memories(SCOPE, "u1")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["id"], first)
        self.assertEqual(memories[0]["source"], "explicit")
        self.assertEqual(memories[0]["source_ids"], [])
        self.assertEqual(self.store.recent_history(SCOPE, "u1"), [])

    def test_history_correction_and_deletion_invalidate_summary(self):
        ref = self.history()
        self.store.add_history(SCOPE, "u1", "assistant", "引用了旧信息", "e1:reply")
        self.store.add_memory(SCOPE, "u1", "旧摘要", source="inferred", source_ids=[ref])
        with self.assertRaises(PermissionError):
            self.store.update_history(ref, SCOPE, "u2", "恶意更改")
        self.store.update_history(ref, SCOPE, "u1", "我现在喜欢 Apex")
        self.assertEqual(self.store.list_memories(SCOPE, "u1"), [])
        self.assertEqual(len(self.store.recent_history(SCOPE, "u1")), 1)
        self.assertEqual(self.store.recent_history(SCOPE, "u1")[0]["text"], "我现在喜欢 Apex")
        self.store.add_memory(SCOPE, "u1", "新摘要", source="inferred", source_ids=[ref])
        self.store.delete_history(ref, SCOPE, "u1")
        self.assertEqual(self.store.stats()["memories"], 0)
        self.assertEqual(self.store.stats()["history"], 0)

    def test_disabling_consent_preserves_only_explicit_memory(self):
        ref = self.history()
        self.store.add_memory(SCOPE, "u1", "自动摘要", source="inferred", source_ids=[ref])
        self.store.add_memory(SCOPE, "u1", "请记住的偏好")
        self.store.set_consent(SCOPE, "u1", False)
        self.assertEqual(self.store.recent_history(SCOPE, "u1"), [])
        self.assertEqual([r["text"] for r in self.store.list_memories(SCOPE, "u1")], ["请记住的偏好"])
        self.store.forget_me(SCOPE, "u1")
        self.assertEqual(self.store.list_memories(SCOPE, "u1"), [])
        self.assertFalse(self.store.consent(SCOPE, "u1"))

    def test_forget_does_not_erase_other_members_or_other_scope(self):
        self.history()
        self.store.add_memory(SCOPE, "u1", "删除")
        self.store.add_memory(SCOPE, "u2", "保留他人")
        self.store.add_memory(OTHER, "u1", "保留其他群")
        result = self.store.forget_me(SCOPE, "u1")
        self.assertEqual(result, {"memories": 1, "history": 1})
        self.assertEqual(self.store.stats()["memories"], 2)

    def test_bounded_history_dedup_and_summary_candidates(self):
        self.store.set_consent(SCOPE, "u1", True)
        for n in range(8):
            self.store.add_history(SCOPE, "u1", "user", f"第 {n} 条", f"e{n}")
        first = self.store.add_history(SCOPE, "u1", "user", "重复事件", "e7")
        second = self.store.add_history(SCOPE, "u1", "user", "又重复事件", "e7")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.recent_history(SCOPE, "u1")), 4)
        self.store.add_memory(SCOPE, "u1", "第7条摘要", source="inferred", source_ids=[first])
        self.store.add_history(SCOPE, "u1", "assistant", "模型生成不能当事实", "e8")
        candidates = self.store.summary_candidates(SCOPE, "u1")
        self.assertTrue(all(r["role"] == "user" and r["id"] != first for r in candidates))

    def test_sql_injection_text_and_chinese_search(self):
        self.store.add_memory(SCOPE, "u1", "喜欢玩无畏契约；DROP TABLE memories; --")
        self.store.add_memory(SCOPE, "u1", "喜欢吃苹果")
        result = self.store.retrieve(SCOPE, "u1", "无畏契约")
        self.assertEqual(len(result), 1)
        self.assertIn("无畏契约", result[0]["text"])
        self.assertEqual(self.store.retrieve(SCOPE, "u1", "完全不相关"), [])

    def test_parallel_replays_remain_one_record(self):
        self.store.set_consent(SCOPE, "u1", True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(lambda _: self.store.add_history(SCOPE, "u1", "user", "同一事件", "same"), range(20)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.store.stats()["history"], 1)

    def test_durable_backup_and_file_permissions(self):
        self.store.add_memory(SCOPE, "u1", "重启后存在")
        backup = Path(self.temp.name) / "snapshot.sqlite3"
        self.store.backup(backup)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        restored = MemoryStore(backup)
        try:
            self.assertEqual(restored.list_memories(SCOPE, "u1")[0]["text"], "重启后存在")
        finally:
            restored.close()
        with self.assertRaises(FileExistsError):
            self.store.backup(backup)


if __name__ == "__main__":
    unittest.main()
