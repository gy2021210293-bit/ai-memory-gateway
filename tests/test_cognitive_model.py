import logging
import sys
import types
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch


asyncpg = types.ModuleType("asyncpg")
asyncpg.Pool = object
sys.modules.setdefault("asyncpg", asyncpg)

jieba = types.ModuleType("jieba")
jieba.__path__ = []
jieba.logging = logging
jieba.setLogLevel = lambda _level: None
jieba.add_word = lambda _word: None
jieba_analyse = types.ModuleType("jieba.analyse")
jieba_analyse.extract_tags = lambda text, topK=10: [text]
jieba.analyse = jieba_analyse
sys.modules.setdefault("jieba", jieba)
sys.modules.setdefault("jieba.analyse", jieba_analyse)

import database


class CognitiveModelTests(unittest.TestCase):
    def test_normalize_accepts_all_four_sections(self):
        examples = {
            "user": "user_core",
            "self": "self_core",
            "relationship": "relationship_core",
            "context": "current_field",
        }
        with patch.object(database, "_local_today", return_value=date(2026, 7, 30)):
            for subject, cognitive_type in examples.items():
                item = database.normalize_cognitive_item_input({
                    "subject": subject,
                    "cognitive_type": cognitive_type,
                    "content": "  有证据的认知  ",
                    "confidence": 0.8,
                    "evidence_memory_ids": [3, "4", 3, "bad"],
                })
                self.assertEqual(item["subject"], subject)
                self.assertEqual(item["content"], "有证据的认知")
                self.assertEqual(item["evidence_memory_ids"], [3, 4])
                expected_review = date(2026, 8, 13) if cognitive_type == "current_field" else None
                self.assertEqual(item["review_after"], expected_review)

    def test_normalize_rejects_unknown_subject_and_type(self):
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "entity", "cognitive_type": "user_core", "content": "x",
            })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "self", "cognitive_type": "invented", "content": "x",
            })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "self", "cognitive_type": "user_core", "content": "x",
            })

    def test_normalize_rejects_legacy_slot_with_refresh_message(self):
        with self.assertRaisesRegex(ValueError, "请刷新页面"):
            database.normalize_cognitive_item_input({
                "subject": "user",
                "cognitive_type": "user_traits_preferences",
                "content": "旧槽位",
            })

    def test_current_field_review_date_can_be_explicit(self):
        item = database.normalize_cognitive_item_input({
            "subject": "context",
            "cognitive_type": "current_field",
            "content": "近期安排",
            "review_after": "2026-08-05",
        })
        self.assertEqual(item["review_after"], date(2026, 8, 5))

    def test_normalize_keeps_long_item_content(self):
        item = database.normalize_cognitive_item_input({
            "subject": "self", "cognitive_type": "self_core", "content": "栖" * 300,
        })
        self.assertEqual(len(item["content"]), 300)

    def test_prompt_does_not_truncate_a_selected_item(self):
        content = "详细认知" * 80
        prompt = database.format_cognitive_items_for_prompt([{
            "subject": "user", "cognitive_type": "user_core",
            "content": content, "status": "active",
        }])
        self.assertIn(content, prompt)

    def test_prompt_groups_three_cores_and_current_field(self):
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_core",
             "content": "偏好简短回答", "confidence": 0.8},
            {"subject": "self", "cognitive_type": "self_core",
             "content": "重视诚实", "confidence": 0.9},
            {"subject": "relationship", "cognitive_type": "relationship_core",
             "content": "共同检查证据"},
            {"subject": "context", "cognitive_type": "current_field",
             "content": "正在准备旅行", "review_after": date(2026, 8, 13)},
        ], today=date(2026, 7, 30))
        self.assertIn("三元一场认知模型", prompt)
        self.assertIn("用户核心｜置信度 0.80", prompt)
        self.assertIn("AI 自我核心", prompt)
        self.assertIn("关系核心", prompt)
        self.assertIn("当前认知场", prompt)
        self.assertIn("当前用户消息", prompt)

    def test_prompt_uses_fixed_order_without_total_budget(self):
        items = [
            {
                "subject": database.COGNITIVE_TYPE_SUBJECTS[cognitive_type],
                "cognitive_type": cognitive_type,
                "content": cognitive_type + "-" + "x" * 300,
                "status": "active",
            }
            for cognitive_type in reversed(database.COGNITIVE_TYPE_ORDER)
        ]
        prompt = database.format_cognitive_items_for_prompt(items)
        labels = ["用户核心", "AI 自我核心", "关系核心", "当前认知场"]
        positions = [prompt.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertGreater(len(prompt), 1200)

    def test_prompt_keeps_only_one_item_per_section(self):
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "context", "cognitive_type": "current_field",
             "content": "较早状态", "status": "active"},
            {"subject": "context", "cognitive_type": "current_field",
             "content": "重复状态", "status": "active"},
        ])
        self.assertIn("较早状态", prompt)
        self.assertNotIn("重复状态", prompt)

    def test_stale_current_field_is_still_injected_with_warning(self):
        item = {
            "subject": "context",
            "cognitive_type": "current_field",
            "content": "旧的当前状态",
            "confidence": 0.6,
            "review_after": date(2026, 7, 30),
        }
        prompt = database.format_cognitive_items_for_prompt([item], today=date(2026, 7, 30))
        self.assertTrue(database.is_cognitive_item_stale(item, today=date(2026, 7, 30)))
        self.assertIn("旧的当前状态", prompt)
        self.assertIn("可能过时，只能作为背景", prompt)
        self.assertNotIn("evidence_memory_ids", prompt)


class _FakeTransaction:
    def __init__(self):
        self.exc_type = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        self.exc_type = exc_type
        return False


class _FakeMigrationConnection:
    def __init__(self, rows, fail_on_insert=False):
        self.rows = rows
        self.fail_on_insert = fail_on_insert
        self.executions = []
        self.transaction_context = _FakeTransaction()

    def transaction(self):
        return self.transaction_context

    async def fetch(self, _query, *_args):
        return self.rows

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.executions.append((normalized, args))
        if self.fail_on_insert and normalized.startswith("INSERT INTO cognitive_items"):
            raise RuntimeError("insert failed")
        return "OK"


class CognitiveMigrationTests(unittest.IsolatedAsyncioTestCase):
    def legacy_rows(self):
        updated_at = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)
        definitions = [
            (1, "user", "user_traits_preferences", "用户旧核心", 0.9, [1, 2]),
            (2, "user", "user_recent_state", "近期状态", 0.8, [3]),
            (3, "self", "self_identity_commitment", "身份承诺", 0.85, [4]),
            (4, "self", "self_growth_lesson", "成长经验", 0.7, [4, 5]),
            (5, "relationship", "relationship_practice_agreement", "共同约定", 0.95, [6]),
            (6, "relationship", "relationship_change", "关系变化", 0.6, [7]),
        ]
        return [
            {
                "id": item_id,
                "subject": subject,
                "cognitive_type": cognitive_type,
                "content": content,
                "confidence": confidence,
                "evidence_memory_ids": evidence_ids,
                "status": "active",
                "created_by": "manual",
                "created_at": updated_at,
                "updated_at": updated_at,
                "review_after": None,
            }
            for item_id, subject, cognitive_type, content, confidence, evidence_ids in definitions
        ]

    async def test_migration_maps_six_slots_to_four_conservative_revisions(self):
        conn = _FakeMigrationConnection(self.legacy_rows())
        migrated = await database._migrate_cognitive_model_v2(conn)
        inserts = [
            args for query, args in conn.executions
            if query.startswith("INSERT INTO cognitive_items")
        ]
        self.assertEqual(migrated, 4)
        self.assertEqual(
            [args[1] for args in inserts],
            ["user_core", "self_core", "relationship_core", "current_field"],
        )
        self.assertIn("身份与承诺：身份承诺", inserts[1][2])
        self.assertIn("成长与理解：成长经验", inserts[1][2])
        self.assertEqual(inserts[1][3], 0.7)
        self.assertEqual(inserts[1][4], [4, 5])
        self.assertEqual(inserts[3][5], date(2026, 8, 4))
        supersede_args = [
            args for query, args in conn.executions
            if "WHERE id = ANY($1::int[])" in query
        ]
        self.assertIn(([1, 2, 3, 4, 5, 6],), supersede_args)

    async def test_migration_is_idempotent_when_new_sections_are_active(self):
        updated_at = datetime(2026, 7, 30, tzinfo=timezone.utc)
        rows = [
            {
                "id": index,
                "subject": database.COGNITIVE_TYPE_SUBJECTS[cognitive_type],
                "cognitive_type": cognitive_type,
                "content": cognitive_type,
                "confidence": 0.8,
                "evidence_memory_ids": [],
                "status": "active",
                "created_by": "migration",
                "created_at": updated_at,
                "updated_at": updated_at,
                "review_after": None,
            }
            for index, cognitive_type in enumerate(database.COGNITIVE_TYPE_ORDER, start=10)
        ]
        conn = _FakeMigrationConnection(rows)
        self.assertEqual(await database._migrate_cognitive_model_v2(conn), 0)
        self.assertFalse(any(
            query.startswith("INSERT INTO cognitive_items")
            for query, _args in conn.executions
        ))

    async def test_migration_failure_propagates_out_of_transaction(self):
        conn = _FakeMigrationConnection(self.legacy_rows(), fail_on_insert=True)
        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            await database._migrate_cognitive_model_v2(conn)
        self.assertIs(conn.transaction_context.exc_type, RuntimeError)


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


class _FakeSaveConnection:
    def __init__(self):
        self.transaction_context = _FakeTransaction()
        self.executions = []

    def transaction(self):
        return self.transaction_context

    async def fetch(self, _query, evidence_ids):
        return [{"id": memory_id} for memory_id in evidence_ids]

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT id, subject"):
            return {
                "id": args[0], "subject": "user",
                "cognitive_type": "user_core", "status": "active",
            }
        if normalized.startswith("INSERT INTO cognitive_items"):
            return {
                "id": 99, "subject": args[0], "cognitive_type": args[1],
                "content": args[2], "confidence": args[3],
                "evidence_memory_ids": args[4], "review_after": args[5],
                "status": "active", "created_by": "manual",
            }
        raise AssertionError(normalized)

    async def execute(self, query, *args):
        self.executions.append((" ".join(query.split()), args))
        return "UPDATE 1"


class CognitiveSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_supersedes_active_row_and_inserts_new_revision(self):
        conn = _FakeSaveConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user",
                "cognitive_type": "user_core",
                "content": "新的用户核心",
                "confidence": 0.8,
                "evidence_memory_ids": [2, 3],
            }, item_id=12)
        self.assertEqual(result["item"]["id"], 99)
        self.assertTrue(any(
            "SET status = 'superseded'" in query and args == ("user_core",)
            for query, args in conn.executions
        ))
        self.assertIsNone(result["item"]["review_after"])


class _FakeEvidenceConnection:
    def __init__(self):
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query = " ".join(query.split())
        self.args = args
        return []


class CognitiveEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_evidence_uses_sixty_high_signal_plus_twenty_recent(self):
        conn = _FakeEvidenceConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            self.assertEqual(await database.get_memories_for_cognitive_draft(80), [])
        self.assertEqual(conn.args, (60, 20, 80))
        self.assertIn("WITH high_signal AS", conn.query)
        self.assertIn("recent AS", conn.query)
        self.assertIn("NOT EXISTS", conn.query)


if __name__ == "__main__":
    unittest.main()
