import logging
import sys
import types
import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch


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
    def test_normalize_accepts_all_three_scopes(self):
        examples = {
            "user": "user_core",
            "self": "self_core",
            "relationship": "relationship_core",
        }
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
            # 未显式给 review_after 时不再自动默认；稳定/当前由前端开关决定
            self.assertIsNone(item["review_after"])

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

    def test_review_date_can_be_explicit_on_any_scope(self):
        for subject, cognitive_type in (
            ("user", "user_core"),
            ("self", "self_core"),
            ("relationship", "relationship_core"),
        ):
            item = database.normalize_cognitive_item_input({
                "subject": subject,
                "cognitive_type": cognitive_type,
                "content": "近期安排",
                "review_after": "2026-08-05",
            })
            self.assertEqual(item["review_after"], date(2026, 8, 5))

    def test_normalize_keeps_long_item_content(self):
        item = database.normalize_cognitive_item_input({
            "subject": "self", "cognitive_type": "self_core", "content": "栖" * 300,
        })
        self.assertEqual(len(item["content"]), 300)

    def test_normalize_accepts_level_and_defaults_action_to_create(self):
        item = database.normalize_cognitive_item_input({
            "subject": "user", "cognitive_type": "user_core",
            "content": "偏好记录", "level": "inductive", "confidence": 0.5,
        })
        self.assertEqual(item["level"], "inductive")
        self.assertEqual(item["action"], "create")
        self.assertIsNone(item["target_id"])

    def test_normalize_rejects_invalid_level_and_action(self):
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "user", "cognitive_type": "user_core",
                "content": "x", "level": "invented",
            })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "user", "cognitive_type": "user_core",
                "content": "x", "action": "bogus",
            })

    def test_normalize_requires_target_id_for_reinforce_supersede_or_conflict(self):
        for action in ("reinforce", "supersede", "conflict"):
            with self.assertRaises(ValueError):
                database.normalize_cognitive_item_input({
                    "subject": "user", "cognitive_type": "user_core",
                    "content": "x", "action": action,
                })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "user", "cognitive_type": "user_core",
                "content": "x", "action": "supersede", "target_id": "bad",
            })
        item = database.normalize_cognitive_item_input({
            "subject": "user", "cognitive_type": "user_core",
            "content": "x", "action": "conflict", "target_id": 7,
        })
        self.assertEqual(item["action"], "conflict")
        self.assertEqual(item["target_id"], 7)

    def test_normalize_merge_requires_at_least_two_targets(self):
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "user", "cognitive_type": "user_core",
                "content": "x", "action": "merge", "target_ids": [3],
            })
        with self.assertRaises(ValueError):
            database.normalize_cognitive_item_input({
                "subject": "user", "cognitive_type": "user_core",
                "content": "x", "action": "merge",
            })
        item = database.normalize_cognitive_item_input({
            "subject": "user", "cognitive_type": "user_core",
            "content": "x", "action": "merge", "target_ids": [3, "4", 3],
        })
        self.assertEqual(item["action"], "merge")
        self.assertEqual(item["target_ids"], [3, 4])

    def test_prompt_does_not_truncate_a_selected_item(self):
        content = "详细认知" * 80
        prompt = database.format_cognitive_items_for_prompt([{
            "subject": "user", "cognitive_type": "user_core",
            "content": content, "status": "active",
        }])
        self.assertIn(content, prompt)

    def test_prompt_groups_three_scopes_and_stability_markers(self):
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_core",
             "content": "偏好简短回答", "confidence": 0.8,
             "level": "explicit", "times_derived": 2},
            {"subject": "self", "cognitive_type": "self_core",
             "content": "重视诚实", "confidence": 0.9,
             "level": "deductive", "times_derived": 1},
            {"subject": "relationship", "cognitive_type": "relationship_core",
             "content": "共同检查证据", "confidence": 0.95},
            {"subject": "user", "cognitive_type": "user_core",
             "content": "正在准备旅行", "confidence": 0.8,
             "review_after": date(2026, 8, 13)},
            {"subject": "user", "cognitive_type": "user_core",
             "content": "可能喜欢咖啡", "confidence": 0.5,
             "level": "inductive", "times_derived": 1},
        ], today=date(2026, 7, 30))
        self.assertIn("三元一场认知模型", prompt)
        self.assertIn("【用户核心】", prompt)
        # 只标例外：明确陈述/推断不再区分层级，高置信长期卡不标前缀
        self.assertIn("- 偏好简短回答", prompt)
        self.assertIn("- 重视诚实", prompt)
        # 低置信 → 标 [低置信]
        self.assertIn("[低置信] 可能喜欢咖啡", prompt)
        self.assertIn("【AI 自我核心】", prompt)
        self.assertIn("【关系核心】", prompt)
        self.assertNotIn("当前认知场", prompt)
        # 不再罗嗦地给每张卡标"层级·置信度"
        self.assertNotIn("[明确陈述·置信度", prompt)
        # 未到期的 current 卡带稳定度标记
        self.assertIn("正在准备旅行（当前状态）", prompt)
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
        labels = ["用户核心", "AI 自我核心", "关系核心"]
        positions = [prompt.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertGreater(len(prompt), 1000)

    def test_prompt_injects_all_cards_without_cap(self):
        # 全量注入：不再有每格 3 张的上限，所有 active 卡都进提示词
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_core",
             "content": "较早状态", "level": "explicit", "times_derived": 1, "id": 1},
            {"subject": "user", "cognitive_type": "user_core",
             "content": "重复状态", "level": "explicit", "times_derived": 1, "id": 2},
            {"subject": "user", "cognitive_type": "user_core",
             "content": "第四状态", "level": "explicit", "times_derived": 1, "id": 4},
            {"subject": "user", "cognitive_type": "user_core",
             "content": "推断状态", "level": "inductive", "times_derived": 9, "id": 3},
        ])
        for content in ("较早状态", "重复状态", "第四状态", "推断状态"):
            self.assertIn(content, prompt)

    def test_prompt_orders_newest_first_within_section(self):
        # 组内新的在前（id 降序），固定可复现，无优先级含义
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_core",
             "content": "旧卡", "level": "explicit", "id": 1},
            {"subject": "user", "cognitive_type": "user_core",
             "content": "新卡", "level": "explicit", "id": 2},
        ])
        self.assertLess(prompt.index("新卡"), prompt.index("旧卡"))

    def test_context_is_ignored_full_injection(self):
        # context 参数保留仅为兼容，实际忽略：不筛选、不标注相关、不降级保底
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_core",
             "content": "话题相关的卡", "level": "explicit",
             "times_derived": 1, "confidence": 0.5, "id": 2,
             "evidence_memory_ids": [10, 11]},
            {"subject": "user", "cognitive_type": "user_core",
             "content": "完全不相关的卡", "level": "explicit",
             "times_derived": 9, "confidence": 0.9, "id": 1,
             "evidence_memory_ids": [99]},
        ], context={"query": "zzz不命中词", "related_memory_ids": [10]})
        self.assertIn("话题相关的卡", prompt)
        self.assertIn("完全不相关的卡", prompt)
        self.assertNotIn("与当前话题相关", prompt)
        self.assertNotIn("按当前话题筛选", prompt)
        self.assertNotIn("长期核心", prompt)

    def test_context_many_cards_all_injected_deterministically(self):
        items = [
            {"subject": "user", "cognitive_type": "user_core",
             "content": f"同分卡{i}", "level": "explicit",
             "times_derived": 1, "confidence": 0.5, "id": i,
             "evidence_memory_ids": [10]}
            for i in range(1, 5)
        ]
        context = {"query": "zzz不命中词", "related_memory_ids": [10]}
        first = database.format_cognitive_items_for_prompt(list(items), context=context)
        second = database.format_cognitive_items_for_prompt(list(items), context=context)
        self.assertEqual(first, second)  # 可复现
        for i in range(1, 5):
            self.assertIn(f"同分卡{i}", first)
        # 新的在前：同分卡4 在 同分卡1 之前
        self.assertLess(first.index("同分卡4"), first.index("同分卡1"))

    def test_header_is_neutral_reference_not_mandate(self):
        prompt = database.format_cognitive_items_for_prompt([
            {"subject": "user", "cognitive_type": "user_core",
             "content": "示例认知", "level": "explicit",
             "times_derived": 1, "confidence": 0.5, "id": 1},
        ])
        self.assertIn("【三元一场认知模型（参考）】", prompt)
        self.assertNotIn("必须遵守", prompt)
        self.assertNotIn("按当前话题筛选", prompt)
        self.assertNotIn("与当前话题相关", prompt)
        self.assertNotIn("长期核心", prompt)
        self.assertIn("仅供参考", prompt)

    def test_expired_current_card_is_retired_from_injection(self):
        # 临时认知到期自动退休：不再注入（人工续期/删除/转稳定后才会回来）
        item = {
            "subject": "user",
            "cognitive_type": "user_core",
            "content": "旧的当前状态",
            "confidence": 0.6,
            "review_after": date(2026, 7, 30),
        }
        prompt = database.format_cognitive_items_for_prompt([item], today=date(2026, 7, 30))
        self.assertTrue(database.is_cognitive_item_stale(item, today=date(2026, 7, 30)))
        self.assertNotIn("旧的当前状态", prompt)
        self.assertEqual(prompt, "")

    def test_unexpired_current_card_is_injected_with_state_marker(self):
        # 未到期的临时卡照常注入，标"（当前状态）"
        item = {
            "subject": "user",
            "cognitive_type": "user_core",
            "content": "正在准备旅行",
            "confidence": 0.8,
            "review_after": date(2026, 8, 13),
        }
        prompt = database.format_cognitive_items_for_prompt([item], today=date(2026, 7, 30))
        self.assertIn("正在准备旅行", prompt)
        self.assertIn("（当前状态）", prompt)

    def test_current_card_shows_update_date(self):
        # 状态卡标"更新于 X 日"，AI 知道新鲜度
        item = {
            "subject": "user",
            "cognitive_type": "user_core",
            "content": "最近在忙新项目",
            "confidence": 0.8,
            "review_after": date(2026, 8, 20),
            "updated_at": "2026-08-13T08:00:00+00:00",
        }
        prompt = database.format_cognitive_items_for_prompt([item], today=date(2026, 7, 30))
        self.assertIn("最近在忙新项目", prompt)
        self.assertIn("（当前状态，更新于 08月13日）", prompt)


class _FakeTransaction:
    def __init__(self):
        self.exc_type = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        self.exc_type = exc_type
        return False


class _FakeMigrationConnection:
    def __init__(self, rows, fail_on_insert=False, migrated_marker=None):
        self.rows = rows
        self.fail_on_insert = fail_on_insert
        self.migrated_marker = migrated_marker
        self.executions = []
        self.transaction_context = _FakeTransaction()

    def transaction(self):
        return self.transaction_context

    async def fetch(self, _query, *_args):
        return self.rows

    async def fetchval(self, query, *_args):
        if "cognitive_v2_migrated" in query:
            return self.migrated_marker
        return None

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

    async def test_v2_data_migration_skipped_when_marker_present(self):
        """已迁移过的库靠标记跳过数据迁移：不插入、不取代旧卡、不重复折叠。"""
        conn = _FakeMigrationConnection(self.legacy_rows(), migrated_marker="1")
        migrated = await database._migrate_cognitive_model_v2(conn)
        self.assertEqual(migrated, 0)
        self.assertFalse(any(
            query.startswith("INSERT INTO cognitive_items")
            for query, _args in conn.executions
        ))
        self.assertFalse(any(
            "WHERE id = ANY($1::int[])" in query
            for query, _args in conn.executions
        ))

    async def test_v2_does_not_deactivate_multiple_cards_in_same_section(self):
        """回归测试：一区多张 active 卡时，v2 不得把除最新外的卡停用。

        旧逻辑每次启动都会把同 cognitive_type 的多余 active 卡标成 superseded，
        与 v3 之后的多卡设计冲突，导致已确认的卡在部署/重启后消失。
        """
        updated_at = datetime(2026, 7, 30, tzinfo=timezone.utc)
        rows = [
            {
                "id": 10, "subject": "user", "cognitive_type": "user_core",
                "content": "最新一张", "confidence": 0.8,
                "evidence_memory_ids": [], "status": "active",
                "created_by": "manual", "created_at": updated_at,
                "updated_at": updated_at, "review_after": None,
            },
            {
                "id": 11, "subject": "user", "cognitive_type": "user_core",
                "content": "更早一张", "confidence": 0.8,
                "evidence_memory_ids": [], "status": "active",
                "created_by": "manual", "created_at": updated_at,
                "updated_at": updated_at, "review_after": None,
            },
        ]
        conn = _FakeMigrationConnection(rows)
        await database._migrate_cognitive_model_v2(conn)
        # 不得把其中任何一张标成 superseded
        self.assertFalse(any(
            "WHERE id = ANY($1::int[])" in query
            for query, _args in conn.executions
        ))

    async def test_v3_migration_adds_layering_columns_and_drops_unique_index(self):
        conn = _FakeMigrationConnection([])
        await database._migrate_cognitive_model_v3(conn)
        combined = " ".join(query for query, _args in conn.executions)
        self.assertIn("ADD COLUMN IF NOT EXISTS level", combined)
        self.assertIn("ADD COLUMN IF NOT EXISTS times_derived", combined)
        self.assertIn("ADD COLUMN IF NOT EXISTS supersedes", combined)
        self.assertIn("ADD COLUMN IF NOT EXISTS superseded_by", combined)
        self.assertIn("DROP INDEX IF EXISTS idx_cognitive_items_one_active_type", combined)

    async def test_v4_migration_creates_revision_log(self):
        conn = _FakeMigrationConnection([])
        await database._migrate_cognitive_model_v4(conn)
        combined = " ".join(query for query, _args in conn.executions)
        self.assertIn("CREATE TABLE IF NOT EXISTS cognitive_revision_log", combined)
        self.assertIn("'create', 'reinforce', 'supersede'", combined)
        self.assertIn("idx_cognitive_revision_log_type_time", combined)

    async def test_v5_migration_folds_current_field_into_user_core(self):
        conn = _FakeMigrationConnection([])
        await database._migrate_cognitive_model_v5(conn)
        combined = " ".join(query for query, _args in conn.executions)
        self.assertIn("UPDATE cognitive_items", combined)
        self.assertIn("cognitive_type = 'current_field'", combined)
        self.assertIn("UPDATE cognitive_revision_log", combined)
        self.assertIn("'user', 'self', 'relationship'", combined)
        self.assertIn("DROP CONSTRAINT cognitive_items_subject_check", combined)


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


TARGET_CARD = {
    "id": 5, "subject": "user", "cognitive_type": "user_core",
    "content": "用户核心", "level": "explicit", "times_derived": 3,
    "confidence": 0.8, "evidence_memory_ids": [1],
    "review_after": None, "status": "active",
}


class _FakeSaveConnection:
    """Minimal stand-in for the save_cognitive_item query flow."""

    def __init__(self, target=None, duplicate_content=None):
        self.transaction_context = _FakeTransaction()
        self.executions = []
        self.target = target
        self.duplicate_content = duplicate_content

    def transaction(self):
        return self.transaction_context

    async def fetch(self, query, arg):
        normalized = " ".join(query.split())
        if "FROM cognitive_items" in normalized:
            # create 去重查询：默认返回空（不重复），可用 duplicate_content 制造命中
            return [{"content": self.duplicate_content}] if self.duplicate_content else []
        return [{"id": memory_id} for memory_id in arg]

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        if "FOR UPDATE" in normalized:
            return dict(self.target) if self.target else None
        if normalized.startswith("INSERT INTO cognitive_items"):
            return {
                "id": 99, "subject": args[0], "cognitive_type": args[1],
                "content": args[2], "confidence": args[3],
                "evidence_memory_ids": args[4], "review_after": args[5],
                "status": "active",
                "created_by": args[9] if len(args) > 9 else "manual",
                "level": args[6], "times_derived": args[7], "supersedes": args[8],
                "superseded_by": None,
            }
        if normalized.startswith("SELECT id, subject, cognitive_type, content, confidence"):
            base = self.target or TARGET_CARD
            merged = list(dict.fromkeys([*base["evidence_memory_ids"], *[2, 3]]))
            return {
                "id": base["id"], "subject": base["subject"],
                "cognitive_type": base["cognitive_type"], "content": base["content"],
                "confidence": base["confidence"], "evidence_memory_ids": merged,
                "review_after": None, "status": "active", "created_by": "manual",
                "level": "explicit", "times_derived": base["times_derived"] + 1,
                "supersedes": None, "superseded_by": None,
            }
        raise AssertionError(normalized)

    async def execute(self, query, *args):
        self.executions.append((" ".join(query.split()), args))
        return "UPDATE 1"


class CognitiveSaveTests(unittest.IsolatedAsyncioTestCase):
    def supersede_executions(self, conn):
        return [
            args for query, args in conn.executions
            if "SET status = 'superseded'" in query
        ]

    def revision_inserts(self, conn):
        return [
            args for query, args in conn.executions
            if query.startswith("INSERT INTO cognitive_revision_log")
        ]

    async def test_create_inserts_new_card_without_superseding_others(self):
        conn = _FakeSaveConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "新的用户核心", "level": "explicit",
                "confidence": 0.8, "evidence_memory_ids": [2, 3],
            })
        self.assertEqual(result["item"]["id"], 99)
        self.assertIsNone(result["item"]["supersedes"])
        # 新建不再整格替换同类型的旧卡
        self.assertEqual(self.supersede_executions(conn), [])
        # 新建被记录为一次人工确认
        self.assertEqual([rev[3] for rev in self.revision_inserts(conn)], ["create"])
        self.assertEqual(self.revision_inserts(conn)[0][5], "新的用户核心")

    async def test_reinforce_increments_times_derived_and_merges_evidence(self):
        conn = _FakeSaveConnection(target=TARGET_CARD)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "用户核心", "level": "explicit",
                "confidence": 0.8, "evidence_memory_ids": [2, 3],
                "action": "reinforce", "target_id": 5,
            })
        self.assertEqual(result["item"]["times_derived"], 4)
        self.assertEqual(result["item"]["evidence_memory_ids"], [1, 2, 3])
        self.assertFalse(any(
            query.startswith("INSERT INTO cognitive_items")
            for query, _args in conn.executions
        ))
        self.assertEqual(self.supersede_executions(conn), [])
        # 强化被记录，action=reinforce
        self.assertEqual([rev[3] for rev in self.revision_inserts(conn)], ["reinforce"])

    async def test_reinforce_rejects_content_mismatch(self):
        conn = _FakeSaveConnection(target=TARGET_CARD)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "内容被改写", "confidence": 0.8,
                "evidence_memory_ids": [2, 3],
                "action": "reinforce", "target_id": 5,
            })
        self.assertEqual(result["error"], "强化需保持内容一致，如需修改请用取代")

    async def test_supersede_marks_target_and_links_history(self):
        conn = _FakeSaveConnection(target=TARGET_CARD)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "新的用户核心", "level": "deductive",
                "confidence": 0.7, "evidence_memory_ids": [2, 3],
                "action": "supersede", "target_id": 5,
            })
        self.assertEqual(result["item"]["id"], 99)
        self.assertEqual(result["item"]["supersedes"], 5)
        # 新卡沿用旧卡的强化次数
        self.assertEqual(result["item"]["times_derived"], 3)
        self.assertEqual(self.supersede_executions(conn), [(5, 99)])
        # 取代被记录，保留旧→新内容
        rev = self.revision_inserts(conn)[0]
        self.assertEqual(rev[3], "supersede")
        self.assertEqual(rev[4], "用户核心")     # content_before
        self.assertEqual(rev[5], "新的用户核心")  # content_after
        self.assertEqual(rev[6], "explicit")     # level_before
        self.assertEqual(rev[7], "deductive")    # level_after

    async def test_edit_supersedes_only_the_target_card(self):
        conn = _FakeSaveConnection(target=TARGET_CARD)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "编辑后的用户核心", "confidence": 0.8,
                "evidence_memory_ids": [2, 3],
            }, item_id=5)
        self.assertEqual(result["item"]["id"], 99)
        self.assertEqual(result["item"]["supersedes"], 5)
        # 只取代被编辑的这一张，不碰同区块的其他卡
        self.assertEqual(self.supersede_executions(conn), [(5, 99)])
        # 手动编辑被记录为 action=edit
        self.assertEqual([rev[3] for rev in self.revision_inserts(conn)], ["edit"])

    async def test_edit_current_card_to_stable_does_not_inherit_review_date(self):
        # 手动把"当前"切成"稳定"保存（不带 review_after）→ 升级为长期，不再被旧复核日期弹回
        current_target = dict(TARGET_CARD, review_after=date(2026, 8, 20))
        conn = _FakeSaveConnection(target=current_target)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "稳定版用户核心", "confidence": 0.8,
                "evidence_memory_ids": [2, 3],
            }, item_id=5)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["item"]["review_after"])  # 升级成功：无复核日期 = 长期卡

    async def test_create_rejects_duplicate_content_in_same_scope(self):
        conn = _FakeSaveConnection(duplicate_content="新的用户核心")
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "新的用户核心", "level": "explicit",
                "confidence": 0.8, "evidence_memory_ids": [2, 3],
            })
        self.assertEqual(result["error"], "该区块已存在相同内容的认知，请用强化或取代")

    async def test_conflict_must_be_resolved_before_save(self):
        conn = _FakeSaveConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "新证据", "level": "explicit",
                "confidence": 0.6, "evidence_memory_ids": [2, 3],
                "action": "conflict", "target_id": 5,
            })
        self.assertEqual(result["error"], "冲突需先裁决为具体动作（保留/取代/新建/修正）")

    async def test_merge_combines_cards_into_one_and_retires_targets(self):
        targets = [
            {"id": 3, "subject": "user", "cognitive_type": "user_core",
             "content": "喜欢安静", "level": "explicit", "times_derived": 1,
             "confidence": 0.7, "evidence_memory_ids": [1], "review_after": None,
             "status": "active"},
            {"id": 4, "subject": "user", "cognitive_type": "user_core",
             "content": "偏好独处", "level": "inductive", "times_derived": 2,
             "confidence": 0.6, "evidence_memory_ids": [2], "review_after": None,
             "status": "active"},
        ]

        class _MergeConn:
            def __init__(self):
                self.transaction_context = _FakeTransaction()
                self.executions = []

            def transaction(self):
                return self.transaction_context

            async def fetch(self, _query, arg):
                return [{"id": mid} for mid in (arg or [])]

            async def fetchrow(self, query, *args):
                normalized = " ".join(query.split())
                if "FOR UPDATE" in normalized:
                    return next((t for t in targets if t["id"] == args[0]), None)
                if normalized.startswith("INSERT INTO cognitive_items"):
                    return {
                        "id": 99, "subject": args[0], "cognitive_type": args[1],
                        "content": args[2], "confidence": args[3],
                        "evidence_memory_ids": args[4], "review_after": args[5],
                        "status": "active", "created_by": args[9],
                        "level": args[6], "times_derived": args[7],
                        "supersedes": args[8], "superseded_by": None,
                    }
                raise AssertionError(normalized)

            async def execute(self, query, *args):
                self.executions.append((" ".join(query.split()), args))
                return "UPDATE 2"

        conn = _MergeConn()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "偏好安静与独处", "level": "explicit",
                "confidence": 0.8, "evidence_memory_ids": [3],
                "action": "merge", "target_ids": [3, 4],
            })
        self.assertEqual(result["item"]["id"], 99)
        self.assertEqual(result["item"]["supersedes"], 3)
        # 两张旧卡全部标记 superseded
        superseded = [
            args for query, args in conn.executions
            if "SET status = 'superseded'" in query
        ]
        self.assertEqual(superseded[0][0], [3, 4])
        # 修订日志记 merge
        revisions = [
            args for query, args in conn.executions
            if query.startswith("INSERT INTO cognitive_revision_log")
        ]
        self.assertEqual(revisions[0][3], "merge")
        self.assertIn("喜欢安静", revisions[0][4])   # content_before 含两张旧卡
        self.assertIn("偏好独处", revisions[0][4])
        self.assertEqual(revisions[0][5], "偏好安静与独处")

    async def test_retire_marks_target_superseded_without_new_card(self):
        conn = _FakeSaveConnection(target=TARGET_CARD)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "我是她愿意倾诉的对象", "level": "explicit",
                "confidence": 0.8, "evidence_memory_ids": [2],
                "action": "retire", "target_id": 5, "retain_id": 3,
            })
        self.assertEqual(result["status"], "ok")
        # 只退休目标卡，不插入新卡
        self.assertFalse(any(
            query.startswith("INSERT INTO cognitive_items")
            for query, _args in conn.executions
        ))
        superseded = [
            args for query, args in conn.executions
            if "SET status = 'superseded'" in query
        ]
        self.assertEqual(superseded[0][0], 5)
        self.assertEqual(superseded[0][1], 3)  # superseded_by = retain_id
        self.assertEqual([rev[3] for rev in self.revision_inserts(conn)], ["retire"])


class _FakeDeleteConnection:
    def __init__(self, row=None):
        self.transaction_context = _FakeTransaction()
        self.executions = []
        self.row = row or {
            "id": 5, "subject": "user", "cognitive_type": "user_core",
            "content": "要删除的认知", "level": "explicit",
        }

    def transaction(self):
        return self.transaction_context

    async def fetchrow(self, _query, _item_id):
        return self.row

    async def execute(self, query, *args):
        self.executions.append((" ".join(query.split()), args))
        return "DELETE 1"


class _FakeRevisionsConnection:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, _query, _limit):
        return self.rows


class CognitiveRevisionLogTests(unittest.IsolatedAsyncioTestCase):
    def revision_inserts(self, conn):
        return [
            args for query, args in conn.executions
            if query.startswith("INSERT INTO cognitive_revision_log")
        ]

    async def test_delete_records_human_delete_decision(self):
        conn = _FakeDeleteConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.delete_cognitive_item(5)
        self.assertEqual(result["status"], "ok")
        executed_queries = [query for query, _args in conn.executions]
        self.assertTrue(any("UPDATE cognitive_items SET supersedes = NULL" in q for q in executed_queries))
        self.assertTrue(any("UPDATE cognitive_items SET superseded_by = NULL" in q for q in executed_queries))
        self.assertTrue(any("UPDATE cognitive_pending SET target_id = NULL" in q for q in executed_queries))
        self.assertTrue(any("DELETE FROM cognitive_items" in q for q in executed_queries))
        rev = self.revision_inserts(conn)[0]
        self.assertEqual(rev[0], 5)                 # card_id
        self.assertEqual(rev[3], "delete")
        self.assertEqual(rev[4], "要删除的认知")     # content_before
        self.assertEqual(rev[6], "explicit")        # level_before

    async def test_rejection_is_recorded_for_feedback(self):
        conn = _FakeDeleteConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.record_cognitive_rejection("user", "user_core", "被拒绝的候选")
        self.assertEqual(result["status"], "ok")
        rev = self.revision_inserts(conn)[0]
        self.assertIsNone(rev[0])                    # 无 card_id
        self.assertEqual(rev[3], "reject")
        self.assertEqual(rev[4], "被拒绝的候选")

    async def test_recent_revisions_fetch_newest_first(self):
        conn = _FakeRevisionsConnection([
            {
                "id": 2, "card_id": None, "subject": "user",
                "cognitive_type": "user_core", "action": "reject",
                "content_before": "被拒", "content_after": None,
                "level_before": None, "level_after": None,
                "created_at": "2026-08-13T00:00:00+00:00",
            },
        ])
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            items = await database.get_recent_cognitive_revisions(10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "reject")

    async def test_delete_revision_removes_audit_record(self):
        conn = _FakeDeleteConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.delete_cognitive_revision(42)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any(
            query.startswith("DELETE FROM cognitive_revision_log")
            for query, _args in conn.executions
        ))

    async def test_delete_revision_missing_returns_error(self):
        conn = _FakeDeleteConnection()
        conn.row = None  # fetchrow 返回空 → 记录不存在
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.delete_cognitive_revision(999)
        self.assertEqual(result["error"], "记录不存在")


class _FakeEvidenceConnection:
    def __init__(self):
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query = " ".join(query.split())
        self.args = args
        return []


class CognitiveEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_evidence_is_new_since_cursor_only(self):
        conn = _FakeEvidenceConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            with patch.object(database, "get_gateway_config",
                             new=AsyncMock(return_value="137")):
                self.assertEqual(await database.get_memories_for_cognitive_draft(80), [])
        self.assertEqual(conn.args, (137, database.COGNITIVE_DRAFT_NEW_LIMIT))
        self.assertIn("id > $1", conn.query)
        self.assertIn("layer = 1", conn.query)
        self.assertNotIn("recent AS", conn.query)
        self.assertNotIn("high_signal AS", conn.query)

    async def test_draft_cursor_zero_when_no_bookmark(self):
        conn = _FakeEvidenceConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            with patch.object(database, "get_gateway_config",
                             new=AsyncMock(return_value="0")):
                self.assertEqual(await database.get_memories_for_cognitive_draft(80), [])
        self.assertEqual(conn.args, (0, database.COGNITIVE_DRAFT_NEW_LIMIT))

    async def test_deep_review_uses_own_cursor_and_long_term_layers(self):
        conn = _FakeEvidenceConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            with patch.object(database, "get_gateway_config",
                             new=AsyncMock(return_value="42")):
                self.assertEqual(await database.get_memories_for_portrait_review(120), [])
        self.assertEqual(conn.args, (42, 6, 120))
        self.assertIn("layer IN (2, 3, 4)", conn.query)

    async def test_advance_deep_cursor_does_not_use_fast_cursor_key(self):
        setter = AsyncMock()
        with patch.object(database, "get_gateway_config",
                          new=AsyncMock(return_value="7")):
            with patch.object(database, "set_gateway_config", new=setter):
                advanced = await database.advance_cognitive_deep_review_cursor([9])
        self.assertEqual(advanced, 9)
        setter.assert_awaited_once_with(database.COGNITIVE_DEEP_REVIEW_CURSOR_KEY, "9")

    async def test_advance_cursor_moves_forward_only(self):
        setter = AsyncMock()
        with patch.object(database, "get_gateway_config",
                         new=AsyncMock(return_value="137")):
            with patch.object(database, "set_gateway_config", new=setter):
                # 这批没有更新的记忆 → 书签保持 137，不写库
                advanced = await database.advance_cognitive_draft_cursor([100, 50])
                setter.assert_not_awaited()
                self.assertEqual(advanced, 137)
                # 有更新的记忆 → 书签推进到 180
                advanced = await database.advance_cognitive_draft_cursor([180, 150, 137])
                setter.assert_awaited_once_with(
                    database.COGNITIVE_DRAFT_CURSOR_KEY, "180")
                self.assertEqual(advanced, 180)

    async def test_advance_cursor_ignores_empty(self):
        with patch.object(database, "get_gateway_config",
                         new=AsyncMock(return_value="0")):
            self.assertEqual(await database.advance_cognitive_draft_cursor([]), 0)

    async def test_list_cognitive_items_reviewed_only_filters_auto(self):
        # 注入门控：reviewed_only=True 时 SQL 必须排除 created_by='auto'（未人工审核）
        conn = _FakeEvidenceConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            await database.list_cognitive_items(active_only=True, reviewed_only=True)
        self.assertIn("status = 'active'", conn.query)
        self.assertIn("created_by <> 'auto'", conn.query)

    async def test_list_cognitive_items_without_reviewed_only_keeps_all(self):
        conn = _FakeEvidenceConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            await database.list_cognitive_items(active_only=True)
        self.assertIn("status = 'active'", conn.query)
        self.assertNotIn("created_by <> 'auto'", conn.query)


class _FakePendingConnection:
    """Fake conn for cognitive_pending queue flows.

    claim_row: row returned by the atomic claim UPDATE ... RETURNING *;
    fetch_rows: rows returned by list_cognitive_pending / get_cognitive_item.
    """

    def __init__(self, claim_row=None, fetch_rows=None):
        self.claim_row = claim_row
        self.fetch_rows = fetch_rows or []
        self.executions = []

    def transaction(self):
        return _FakeTransaction()

    async def fetch(self, _query, *_args):
        return self.fetch_rows

    async def fetchrow(self, query, *_args):
        normalized = " ".join(query.split())
        if normalized.startswith("INSERT INTO cognitive_pending"):
            return {"id": 77}
        if "UPDATE cognitive_pending SET status = 'accepted'" in normalized:
            return dict(self.claim_row) if self.claim_row else None
        if "UPDATE cognitive_pending SET status = 'rejected'" in normalized:
            if self.claim_row:
                return {
                    "subject": self.claim_row["subject"],
                    "cognitive_type": self.claim_row["cognitive_type"],
                    "content": self.claim_row["content"],
                }
            return None
        if normalized.startswith("SELECT id, subject, cognitive_type, content, confidence"):
            return dict(self.claim_row) if self.claim_row else None
        return None

    async def execute(self, query, *args):
        self.executions.append((" ".join(query.split()), args))
        return "UPDATE 1"


def _pending_row(action="create", target_id=None, confidence=0.6, level="inductive"):
    return {
        "id": 7, "subject": "user", "cognitive_type": "user_core",
        "content": "低置信度的候选认知", "confidence": confidence,
        "level": level, "action": action, "target_id": target_id,
        "target_ids": None, "evidence_memory_ids": [12], "review_after": None,
        "source": "auto", "status": "pending",
    }


class CognitivePendingQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_persists_normalized_candidate(self):
        conn = _FakePendingConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            pending_id = await database.queue_cognitive_pending({
                "subject": "user", "cognitive_type": "user_core",
                "content": "待确认候选", "level": "inductive",
                "confidence": 0.5, "evidence_memory_ids": [12],
                "action": "create",
            })
        self.assertEqual(pending_id, 77)

    async def test_list_returns_pending_rows(self):
        conn = _FakePendingConnection(fetch_rows=[_pending_row()])
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            items = await database.list_cognitive_pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 7)

    async def test_accept_applies_as_manual_and_marks_accepted(self):
        conn = _FakePendingConnection(claim_row=_pending_row())
        save_mock = AsyncMock(return_value={"status": "ok", "item": {"id": 99}})
        with (
            patch.object(database, "get_pool", return_value=_FakePool(conn)),
            patch.object(database, "save_cognitive_item", new=save_mock),
        ):
            result = await database.accept_cognitive_pending(7)
        self.assertEqual(result["status"], "ok")
        save_mock.assert_awaited_once()
        self.assertEqual(save_mock.await_args.kwargs["created_by"], "manual")
        payload = save_mock.await_args.args[0]
        self.assertEqual(payload["action"], "create")

    async def test_accept_conflict_requires_resolve(self):
        conn = _FakePendingConnection(claim_row=_pending_row(action="conflict", target_id=5))
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.accept_cognitive_pending(7)
        self.assertIn("error", result)
        self.assertIn("supersede", result["error"])
        # 裁决参数非法 → 退回 pending
        self.assertTrue(any(
            "SET status = 'pending'" in query for query, _args in conn.executions
        ))

    async def test_accept_conflict_keep_rejects_and_records(self):
        conn = _FakePendingConnection(claim_row=_pending_row(action="conflict", target_id=5))
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.accept_cognitive_pending(7, resolve="keep")
        self.assertEqual(result["decision"], "kept_old")
        revisions = [
            args for query, args in conn.executions
            if query.startswith("INSERT INTO cognitive_revision_log")
        ]
        self.assertEqual(revisions[0][3], "reject")

    async def test_accept_conflict_supersede_routes_action(self):
        conn = _FakePendingConnection(claim_row=_pending_row(action="conflict", target_id=5))
        save_mock = AsyncMock(return_value={"status": "ok", "item": {"id": 99}})
        with (
            patch.object(database, "get_pool", return_value=_FakePool(conn)),
            patch.object(database, "save_cognitive_item", new=save_mock),
        ):
            result = await database.accept_cognitive_pending(7, resolve="supersede")
        self.assertEqual(result["status"], "ok")
        payload = save_mock.await_args.args[0]
        self.assertEqual(payload["action"], "supersede")
        self.assertEqual(payload["target_id"], 5)

    async def test_accept_apply_failure_reverts_to_pending(self):
        conn = _FakePendingConnection(claim_row=_pending_row())
        save_mock = AsyncMock(return_value={"error": "该区块已存在相同内容的认知"})
        with (
            patch.object(database, "get_pool", return_value=_FakePool(conn)),
            patch.object(database, "save_cognitive_item", new=save_mock),
        ):
            result = await database.accept_cognitive_pending(7)
        self.assertIn("error", result)
        self.assertTrue(any(
            "SET status = 'pending'" in query for query, _args in conn.executions
        ))

    async def test_reject_records_rejection_and_marks_decided(self):
        conn = _FakePendingConnection(claim_row=_pending_row())
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.reject_cognitive_pending(7)
        self.assertEqual(result["status"], "ok")
        revisions = [
            args for query, args in conn.executions
            if query.startswith("INSERT INTO cognitive_revision_log")
        ]
        self.assertEqual(revisions[0][3], "reject")
        self.assertEqual(revisions[0][4], "低置信度的候选认知")

    async def test_reject_missing_returns_error(self):
        conn = _FakePendingConnection(claim_row=None)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.reject_cognitive_pending(999)
        self.assertIn("error", result)

    async def test_auto_save_records_auto_create_revision(self):
        conn = _FakeSaveConnection()
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            result = await database.save_cognitive_item({
                "subject": "user", "cognitive_type": "user_core",
                "content": "自动应用的新卡", "level": "explicit",
                "confidence": 0.85, "evidence_memory_ids": [2, 3],
            }, created_by="auto")
        self.assertEqual(result["item"]["created_by"], "auto")
        self.assertEqual([rev[3] for rev in self.revision_inserts(conn)], ["auto_create"])

    def revision_inserts(self, conn):
        return [
            args for query, args in conn.executions
            if query.startswith("INSERT INTO cognitive_revision_log")
        ]

    async def test_get_cognitive_item_returns_row(self):
        row = {"id": 5, "created_by": "auto", "subject": "user",
               "cognitive_type": "user_core", "content": "x"}
        conn = _FakePendingConnection(claim_row=row)
        with patch.object(database, "get_pool", return_value=_FakePool(conn)):
            item = await database.get_cognitive_item(5)
        self.assertEqual(item["created_by"], "auto")


if __name__ == "__main__":
    unittest.main()
