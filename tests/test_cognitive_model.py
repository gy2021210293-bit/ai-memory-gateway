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
        ], today=date(2026, 7, 30))
        self.assertIn("三元一场认知模型", prompt)
        self.assertIn("【用户核心】", prompt)
        self.assertIn("[明确陈述·强化×2｜置信度0.80] 偏好简短回答", prompt)
        self.assertIn("[演绎推断·强化×1｜置信度0.90] 重视诚实", prompt)
        self.assertIn("【AI 自我核心】", prompt)
        self.assertIn("【关系核心】", prompt)
        self.assertNotIn("当前认知场", prompt)
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

    def test_prompt_keeps_top_cards_per_section_and_drops_lowest_ranked(self):
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
        # 每格最多 COGNITIVE_PER_TYPE_LIMIT 张：明确陈述优先，归纳推断被挤出
        self.assertIn("较早状态", prompt)
        self.assertIn("重复状态", prompt)
        self.assertIn("第四状态", prompt)
        self.assertNotIn("推断状态", prompt)

    def test_stale_current_card_is_still_injected_with_warning(self):
        item = {
            "subject": "user",
            "cognitive_type": "user_core",
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
                "status": "active", "created_by": "manual",
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
