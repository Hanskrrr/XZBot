"""Synthetic lexical-retrieval regressions; no live data or external APIs."""

import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from azusa_bot.core import Engine, Incoming
from azusa_bot.memory import MemoryStore
from azusa_bot.settings import Settings, write_settings


SCOPE = "onebot:10001:group:30001"
USER = "20001"


class MemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(":memory:")

    def tearDown(self):
        self.store.close()

    def save(self, text):
        return self.store.add_memory(SCOPE, USER, text)

    def texts(self, query):
        return [row["text"] for row in self.store.retrieve(SCOPE, USER, query)]

    def test_tokenless_queries_do_not_disclose_latest_profile(self):
        self.save("我养了一只猫")
        self.store.add_memory(SCOPE, "owner", "群活动周末举行", kind="group", is_owner=True)
        for query in ("", "  ", "!!!", "🐱"):
            with self.subTest(query=query):
                self.assertEqual(self.texts(query), [])

    def test_single_han_query_matches_inside_a_longer_run(self):
        self.save("我养了一只猫")
        self.save("最爱瓦洛兰特")
        self.save("周末去跑步")
        self.assertEqual(self.texts("猫"), ["我养了一只猫"])
        self.assertEqual(self.texts("瓦"), ["最爱瓦洛兰特"])
        self.assertEqual(self.texts("🐱 猫！"), ["我养了一只猫"])
        self.assertEqual(self.texts("狗"), [])

    def test_single_han_fallback_keeps_scope_and_subject_permissions(self):
        mine = self.save("我养了一只猫")
        public = self.store.add_memory(SCOPE, "owner", "群里讨论猫", kind="group", is_owner=True)
        self.store.add_memory(SCOPE, "20002", "另一个人的猫")
        for other in ("onebot:10001:group:30002", "onebot:10001:dm:20001",
                      "onebot:10002:group:30001", "qq:10001:group:30001"):
            self.store.add_memory(other, USER, "隔离作用域的猫")
        self.assertEqual({r["id"] for r in self.store.retrieve(SCOPE, USER, "猫")}, {mine, public})
        self.assertEqual({r["id"] for r in self.store.retrieve(SCOPE, "owner", "猫")}, {public})

    def test_multichar_queries_are_not_broadened_to_all_single_characters(self):
        self.save("我养了一只猫")
        self.save("猫咪喜欢趴窗边")
        self.assertEqual(self.texts("猫咪"), ["猫咪喜欢趴窗边"])
        # The single-Han fallback is lexical, not a synonym/alias model.
        self.assertEqual(self.texts("喵星人"), [])
        self.save("最爱瓦洛兰特")
        self.assertEqual(self.texts("VALORANT"), [])
        self.assertEqual(self.texts("无畏契约"), [])

    def test_single_character_document_frequency_counts_literal_matches(self):
        self.save("猫与跑步")
        self.save("猫猫猫")
        self.save("跑步")
        rows = self.store.retrieve(SCOPE, USER, "猫 跑步")
        self.assertEqual(rows[0]["text"], "猫与跑步")
        expected = math.log(1 + 4 / 3)
        self.assertEqual(rows[0]["score"], round(2 * expected, 6))
        self.assertEqual({r["score"] for r in rows[1:]}, {round(expected, 6)})

    def test_corrected_or_deleted_literal_match_is_not_retrieved(self):
        number = self.save("我养了一只猫")
        self.store.update_memory(number, SCOPE, USER, "我养了一只狗")
        self.assertEqual(self.texts("猫"), [])
        self.assertEqual(self.texts("狗"), ["我养了一只狗"])
        self.store.delete_memory(number, SCOPE, USER)
        self.assertEqual(self.texts("狗"), [])


class MemoryCommandContextTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real command route after a confirmed synthetic conversation."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "content").mkdir()
        (root / "content/persona.md").write_text("这是本地测试角色。")
        write_settings(root, Settings(transport="onebot", onebot_enabled=True,
            onebot_self_id="10001", onebot_allowed_groups=["30001", "30002"],
            group_chat_enabled=True, group_chat_debounce_seconds=0, min_interval_seconds=0))
        self.engine = Engine(root)
        self.provider = AsyncMock()
        self.provider.complete.return_value = {"text": "我记得你养了一只猫。", "mode": "text"}
        self.engine.provider_override = self.provider
        self.number = self.engine.memory.add_memory(SCOPE, USER, "我养了一只猫")
        for index, group in enumerate(("30001", "30002")):
            reply = await self.engine.handle(self.event(group, "我养了一只猫", f"start-{index}"))
            self.assertEqual(reply["mode"], "text")
            self.engine.acknowledge(reply["reply_id"], "sent", str(91001 + index))
        self.assertIn((SCOPE, USER), self.engine._group_threads)
        self.assertTrue(self.engine.groups.snapshot(SCOPE))
        self.provider.complete.reset_mock()

    async def asyncTearDown(self):
        self.engine.memory.close()
        self.engine.groups.close()
        self.temp.cleanup()

    @staticmethod
    def event(group, text, event_id):
        return Incoming(transport="onebot", app_id="10001", kind="group", scope_id=group,
                        user_id=USER, event_id=event_id, text=text, mentioned=True)

    async def check_command(self, text):
        reply = await self.engine.handle(self.event("30001", text, "edit-command"))
        self.assertEqual(reply["mode"], "text")
        self.provider.complete.assert_not_awaited()
        self.assertEqual(self.engine.groups.snapshot(SCOPE), [])
        self.assertNotIn((SCOPE, USER), self.engine._group_threads)
        other = "onebot:10001:group:30002"
        self.assertTrue(self.engine.groups.snapshot(other))
        self.assertIn((other, USER), self.engine._group_threads)
        self.assertEqual(self.engine.memory.retrieve(SCOPE, USER, "猫"), [])
        return reply

    async def test_forget_item_command_clears_echoed_fact_and_followup(self):
        reply = await self.check_command(f"/忘记 {self.number}")
        self.assertEqual(reply["text"], "该条记忆已删除。")
        self.assertEqual(self.engine.memory.list_memories(SCOPE, USER), [])

    async def test_correct_item_command_clears_old_fact_and_followup(self):
        reply = await self.check_command(f"/纠正 {self.number} 我养了一只狗")
        self.assertEqual(reply["text"], "该条记忆已更新。")
        found = self.engine.memory.retrieve(SCOPE, USER, "狗")
        self.assertEqual([(row["id"], row["text"]) for row in found], [(self.number, "我养了一只狗")])
