import asyncio
import json
import logging
import sys
import types
import unittest
from pathlib import Path
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


ROOT = Path(__file__).resolve().parents[1]


class AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return AsyncContext(self.conn)


class CardConnection:
    """Fake conn returning a card payload and recording UPDATE writes."""

    def __init__(self, card_json, entity_exists=True, owned_memory_ids=None):
        self.card_json = card_json
        self.entity_exists = entity_exists
        self.owned_memory_ids = owned_memory_ids or []
        self.execute_calls = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, _sql, *_args):
        if not self.entity_exists:
            return None
        return {"entity_card_json": self.card_json}

    async def fetch(self, _sql, *_args):
        return [{"memory_id": mid} for mid in self.owned_memory_ids]

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class EvidenceConnection:
    def __init__(self):
        self.executemany_calls = []

    async def executemany(self, sql, rows):
        self.executemany_calls.append((sql, rows))


class EntityCardTests(unittest.TestCase):
    EMPTY_CARD = {"description": "", "stable_traits": [], "snapshots": []}

    def test_parse_entity_card_empty_defaults(self):
        self.assertEqual(database._parse_entity_card(None), self.EMPTY_CARD)
        self.assertEqual(database._parse_entity_card("not json"), self.EMPTY_CARD)
        self.assertEqual(database._parse_entity_card([]), self.EMPTY_CARD)

    def test_parse_entity_card_accepts_str_and_dict(self):
        payload = {"description": "  朋友  ", "snapshots": [
            {"fact_date": "2026-07-02", "recorded_at": "2026-07-02T00:00:00", "state": "B", "source": "direct"},
            {"fact_date": "2026-07-01", "recorded_at": "2026-07-01T00:00:00", "state": "A", "source": "direct"},
        ]}
        card = database._parse_entity_card(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(card["description"], "朋友")
        self.assertEqual([snap["state"] for snap in card["snapshots"]], ["A", "B"])
        card2 = database._parse_entity_card(payload)
        self.assertEqual(card2["snapshots"][-1]["state"], "B")

    def test_sort_snapshots_preserves_full_history(self):
        snapshots = [
            {"fact_date": f"2026-01-{day:02d}", "recorded_at": "", "state": f"state-{day}", "source": "direct"}
            for day in range(1, 9)
        ]
        ordered = database._sort_snapshots(snapshots)
        # 不设数量上限：全部保留，按日期升序，最后一条永远是最新状态
        self.assertEqual(len(ordered), 8)
        self.assertEqual(ordered[-1]["state"], "state-8")
        self.assertEqual(ordered[0]["state"], "state-1")

    def test_snapshot_conflicts_only_with_tail_same_date(self):
        snapshots = [
            {"fact_date": "2026-07-01", "recorded_at": "", "state": "old", "source": "direct"},
            {"fact_date": "2026-07-10", "recorded_at": "", "state": "current", "source": "direct"},
        ]
        self.assertTrue(database._snapshot_conflicts_with_tail(snapshots, "different", "2026-07-10"))
        self.assertFalse(database._snapshot_conflicts_with_tail(snapshots, "current", "2026-07-10"))
        # 补录旧事：早于末节点、不影响新的当前节点
        self.assertFalse(database._snapshot_conflicts_with_tail(snapshots, "older fact", "2026-07-01"))
        self.assertFalse(database._snapshot_conflicts_with_tail(snapshots, "anything", "2026-07-05"))

    def test_migration_is_guarded_and_idempotent(self):
        source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("column_name = 'entity_card_json'", source)
        self.assertIn("column_name = 'entity_card_updated_at'", source)
        idx = source.index("entity_card_json")
        self.assertIn("IF NOT EXISTS", source[idx - 300:idx])
        self.assertIn("CREATE TABLE IF NOT EXISTS entity_card_proposals", source)
        self.assertIn("CREATE TABLE IF NOT EXISTS memory_evidence", source)
        self.assertIn("status IN ('pending', 'accepted', 'rejected')", source)
        self.assertIn("PRIMARY KEY (memory_id, conversation_id)", source)

    def test_event_and_merge_inherit_fragment_evidence(self):
        source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("INSERT INTO memory_evidence"), 2)
        self.assertIn("FROM memory_evidence WHERE memory_id = ANY($2::int[])", source)

    def test_merge_entities_merges_cards_and_reassigns_proposals(self):
        source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("entity_card_json = $2::jsonb", source)
        self.assertIn("entity_card_proposals SET entity_id = $2", source)
        self.assertIn("status = 'pending'", source)

    def test_card_payload_has_no_current_state_field(self):
        card = database._parse_entity_card({
            "description": "x",
            "snapshots": [{"fact_date": "2026-07-10", "recorded_at": "", "state": "s", "source": "direct"}],
        })
        self.assertNotIn("current_state", card)

    def test_entity_card_summary_reports_card_and_last_date(self):
        self.assertEqual(database._entity_card_summary(None), (False, None))
        self.assertEqual(database._entity_card_summary({"description": "x", "snapshots": []}), (False, None))
        card = {
            "snapshots": [
                {"fact_date": "2026-07-01", "recorded_at": "", "state": "A", "source": "direct"},
                {"fact_date": "2026-07-20", "recorded_at": "", "state": "B", "source": "direct"},
            ]
        }
        self.assertEqual(database._entity_card_summary(card), (True, "2026-07-20"))
        # jsonb 列以字符串返回时同样解析
        self.assertEqual(database._entity_card_summary(json.dumps(card, ensure_ascii=False)), (True, "2026-07-20"))

    def test_card_has_description_is_card_only(self):
        # 卡内说明存在 → True
        self.assertTrue(database._card_has_description({"description": "  朋友  ", "snapshots": []}))
        # 卡内无说明 → False（遗留 entities.description 不计入：卡面 说明 为空就显示 无说明）
        self.assertFalse(database._card_has_description({"description": "", "snapshots": []}))
        self.assertFalse(database._card_has_description(None))
        self.assertFalse(database._card_has_description("not json"))

    def test_card_has_active_traits_only_counts_active(self):
        # 至少一条活跃特征 → True
        self.assertTrue(database._card_has_active_traits({
            "stable_traits": [
                {"id": "t1", "text": "爱喝咖啡", "status": "active"},
                {"id": "t2", "text": "旧爱好", "status": "retired"},
            ],
        }))
        # 只有退休特征 → False（退休特征不再注入聊天，卡面显示 无特征）
        self.assertFalse(database._card_has_active_traits({
            "stable_traits": [{"id": "t1", "text": "旧爱好", "status": "retired"}],
        }))
        # 无 stable_traits 字段 / 非数组 / 缺卡 → False（兼容旧卡与空卡）
        self.assertFalse(database._card_has_active_traits({"description": "x", "snapshots": []}))
        self.assertFalse(database._card_has_active_traits({"stable_traits": "not-an-array"}))
        self.assertFalse(database._card_has_active_traits(None))
        self.assertFalse(database._card_has_active_traits("not json"))

    def test_parse_entity_card_parses_stable_traits(self):
        payload = {
            "description": "朋友",
            "stable_traits": [
                {"id": "t1", "text": "  长期目标是边缘设备部署  ", "status": "active",
                 "first_seen": "2026-03-01", "last_confirmed": "2026-08-11",
                 "evidence_memory_ids": [101, 101, 205], "evidence_message_ids": [3001],
                 "origin": "confirmed"},
                {"id": "", "text": "无ID被丢弃", "status": "active"},
            ],
            "snapshots": [],
        }
        card = database._parse_entity_card(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(card["description"], "朋友")
        self.assertEqual(len(card["stable_traits"]), 1)
        trait = card["stable_traits"][0]
        self.assertEqual(trait["id"], "t1")
        self.assertEqual(trait["text"], "长期目标是边缘设备部署")
        self.assertEqual(trait["evidence_memory_ids"], [101, 205])  # 重复ID去重
        # 旧卡没有 stable_traits 字段 → 空数组（兼容）
        self.assertEqual(database._parse_entity_card({"description": "x", "snapshots": []})["stable_traits"], [])

    def test_parse_entity_card_parses_diary_views(self):
        payload = {
            "description": "",
            "snapshots": [
                {"fact_date": "2026-07-20", "recorded_at": "", "state": "搬到北京",
                 "user_view": "  如释重负  ", "ai_view": "替她高兴", "source": "direct"},
                {"fact_date": "2026-08-01", "recorded_at": "", "state": "入职新公司", "source": "direct"},
            ],
        }
        card = database._parse_entity_card(payload)
        self.assertEqual(card["snapshots"][0]["user_view"], "如释重负")
        self.assertEqual(card["snapshots"][0]["ai_view"], "替她高兴")
        # 无看法的快照 → 空字符串（可选字段）
        self.assertEqual(card["snapshots"][1]["user_view"], "")
        self.assertEqual(card["snapshots"][1]["ai_view"], "")

    def test_clean_int_list_dedupes_and_filters(self):
        self.assertEqual(database._clean_int_list([1, 1, 2, "3", None, 0]), [1, 2, 3])
        self.assertEqual(database._clean_int_list("not-a-list"), [])
        self.assertEqual(database._clean_int_list(None), [])

    def test_merge_stable_traits_dedup_and_conflict(self):
        target = [
            {"id": "a", "text": "长期目标是边缘设备部署", "status": "active",
             "first_seen": "2026-01-01", "last_confirmed": "2026-06-01",
             "evidence_memory_ids": [1], "evidence_message_ids": [11], "origin": "confirmed"},
            {"id": "b", "text": "习惯晨跑", "status": "retired",
             "first_seen": "2026-02-01", "last_confirmed": "2026-03-01",
             "evidence_memory_ids": [2], "evidence_message_ids": [], "origin": "confirmed"},
        ]
        source = [
            {"id": "c", "text": "长期目标是边缘设备部署", "status": "active",
             "first_seen": "2026-02-01", "last_confirmed": "2026-07-01",
             "evidence_memory_ids": [3], "evidence_message_ids": [33], "origin": "candidate"},
            {"id": "d", "text": "习惯晨跑", "status": "active",  # 同文本状态冲突
             "first_seen": "2026-02-01", "last_confirmed": "2026-04-01",
             "evidence_memory_ids": [4], "evidence_message_ids": [], "origin": "confirmed"},
        ]
        merged, conflicts = database._merge_stable_traits(target, source)
        merged_by_text = {t["text"]: t for t in merged}
        # 同文本同状态 → 合并证据 + 最早 first_seen + 最新 last_confirmed
        trait = merged_by_text["长期目标是边缘设备部署"]
        self.assertEqual(sorted(trait["evidence_memory_ids"]), [1, 3])
        self.assertEqual(sorted(trait["evidence_message_ids"]), [11, 33])
        self.assertEqual(trait["first_seen"], "2026-01-01")
        self.assertEqual(trait["last_confirmed"], "2026-07-01")
        # 同文本状态冲突 → 不自行决定，生成冲突描述
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["text"], "习惯晨跑")
        self.assertEqual(conflicts[0]["status_existing"], "retired")
        self.assertEqual(conflicts[0]["status_proposed"], "active")
        self.assertEqual(conflicts[0]["existing_trait_id"], "b")

    def test_list_entities_includes_pending_count_and_card_summary(self):
        calls = []

        async def fake_fetch(sql, *args):
            calls.append((sql, args))
            return [{
                "id": 1, "name": "Alice", "entity_type": "person", "description": "",
                "profile_json": None, "profile_evidence_ids": [], "profile_updated_at": None,
                "profile_model": None, "entity_card_json": None, "entity_card_updated_at": None,
                "evidence_count": 5, "status_override": None, "created_at": None, "updated_at": None,
                "memory_count": 2, "aliases": [], "pending_proposal_count": 1,
            }]

        conn = types.SimpleNamespace(fetch=fake_fetch)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            entities = asyncio.run(database.list_entities())
        self.assertIn("pending_proposal_count", calls[0][0])
        self.assertIn("status = 'pending'", calls[0][0])
        self.assertEqual(entities[0]["pending_proposal_count"], 1)
        self.assertFalse(entities[0]["card_has_snapshots"])
        self.assertIsNone(entities[0]["card_last_state_date"])
        self.assertFalse(entities[0]["card_has_description"])
        self.assertFalse(entities[0]["card_has_active_traits"])

    def test_list_entities_without_card_filters_empty_cards(self):
        calls = []

        async def fake_fetch(sql, *args):
            calls.append((sql, args))
            return []

        conn = types.SimpleNamespace(fetch=fake_fetch)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            asyncio.run(database.list_entities_without_card(5))
        self.assertEqual(len(calls), 1)
        sql = calls[0][0]
        self.assertIn("entity_card_json->'snapshots'", sql)
        self.assertIn("HAVING COUNT(DISTINCT me.memory_id) > 0", sql)
        # 只补可命中（活跃）实体：与检索路径同一过滤条件
        self.assertIn("status_override = 'active'", sql)
        self.assertIn("evidence_count >= 3", sql)
        self.assertIn("LIMIT $1", sql)
        self.assertEqual(calls[0][1], (5,))

    def test_list_entities_without_active_traits_filters_no_active_traits(self):
        calls = []

        async def fake_fetch(sql, *args):
            calls.append((sql, args))
            return []

        conn = types.SimpleNamespace(fetch=fake_fetch)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            asyncio.run(database.list_entities_without_active_traits(5))
        self.assertEqual(len(calls), 1)
        sql = calls[0][0]
        # 只补「卡上没有 active 稳定特征」的实体
        self.assertIn("stable_traits", sql)
        self.assertIn("t->>'status' = 'active'", sql)
        self.assertIn("HAVING COUNT(DISTINCT me.memory_id) > 0", sql)
        # 只补可命中（活跃）实体：与检索路径同一过滤条件
        self.assertIn("status_override = 'active'", sql)
        self.assertIn("LIMIT $1", sql)
        self.assertEqual(calls[0][1], (5,))


class EntityCardAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_snapshot_appends_sorts_and_caps(self):
        conn = CardConnection({
            "description": "", "snapshots": [
                {"fact_date": "2026-07-01", "recorded_at": "2026-07-01T00:00:00", "state": "old", "source": "direct"},
                {"fact_date": "2026-07-10", "recorded_at": "2026-07-10T00:00:00", "state": "newer", "source": "direct"},
            ],
        })
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.apply_entity_snapshot(1, "latest", "2026-07-15", source="confirmed")
        self.assertEqual(result["status"], "ok")
        written = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual([snap["state"] for snap in written["snapshots"]], ["old", "newer", "latest"])
        self.assertEqual(written["snapshots"][-1]["source"], "confirmed")

    async def test_apply_snapshot_duplicate_is_noop(self):
        conn = CardConnection({
            "description": "", "snapshots": [
                {"fact_date": "2026-07-10", "recorded_at": "2026-07-10T00:00:00", "state": "state A", "source": "direct"},
            ],
        })
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.apply_entity_snapshot(1, "state A", "2026-07-10", source="direct")
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(conn.execute_calls, [])

    async def test_apply_snapshot_same_date_tail_conflict_escalates(self):
        conn = CardConnection({
            "description": "", "snapshots": [
                {"fact_date": "2026-07-10", "recorded_at": "2026-07-10T00:00:00", "state": "state A", "source": "direct"},
            ],
        })
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.apply_entity_snapshot(1, "state B", "2026-07-10", source="direct", force=False)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(conn.execute_calls, [])

    async def test_apply_snapshot_force_overrides_tail_conflict(self):
        conn = CardConnection({
            "description": "", "snapshots": [
                {"fact_date": "2026-07-10", "recorded_at": "2026-07-10T00:00:00", "state": "state A", "source": "direct"},
            ],
        })
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.apply_entity_snapshot(1, "state B", "2026-07-10", source="confirmed", force=True)
        self.assertEqual(result["status"], "ok")
        written = json.loads(conn.execute_calls[0][1][1])
        # 人工确认覆盖同日末节点后，重排结果以新快照为末节点（当前状态）
        self.assertEqual(written["snapshots"][-1]["state"], "state B")

    async def test_apply_snapshot_persists_diary_views(self):
        conn = CardConnection({"description": "", "snapshots": []})
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.apply_entity_snapshot(
                1, "搬到北京", "2026-07-20", source="confirmed", force=True,
                user_view="如释重负", ai_view="替她高兴",
            )
        self.assertEqual(result["status"], "ok")
        written = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual(written["snapshots"][0]["user_view"], "如释重负")
        self.assertEqual(written["snapshots"][0]["ai_view"], "替她高兴")

    async def test_backdated_snapshot_does_not_replace_newer_tail(self):
        conn = CardConnection({
            "description": "", "snapshots": [
                {"fact_date": "2026-07-10", "recorded_at": "2026-07-10T00:00:00", "state": "current state", "source": "direct"},
            ],
        })
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.apply_entity_snapshot(1, "older fact", "2026-07-01", source="direct", force=False)
        self.assertEqual(result["status"], "ok")
        written = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual(len(written["snapshots"]), 2)
        self.assertEqual(written["snapshots"][0]["state"], "older fact")
        self.assertEqual(written["snapshots"][-1]["state"], "current state")
        self.assertNotIn("current_state", written)

    async def test_apply_snapshot_requires_entity_and_valid_input(self):
        conn = CardConnection({}, entity_exists=False)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            self.assertEqual((await database.apply_entity_snapshot(9, "s", "2026-07-01"))["status"], "not_found")
        conn2 = CardConnection({})
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn2))):
            self.assertEqual((await database.apply_entity_snapshot(1, "", "2026-07-01"))["status"], "error")
            self.assertEqual((await database.apply_entity_snapshot(1, "s", ""))["status"], "error")

    async def test_add_entity_card_trait_appends(self):
        conn = CardConnection({"description": "", "stable_traits": [], "snapshots": []}, owned_memory_ids=[1, 2])
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.add_entity_card_trait(
                1, "长期目标是边缘设备部署", first_seen="2026-03-01", last_confirmed="2026-08-11",
                evidence_memory_ids=[1, 2],
            )
        self.assertEqual(result["status"], "ok")
        written = json.loads(conn.execute_calls[0][1][1])
        trait = written["stable_traits"][0]
        self.assertEqual(trait["text"], "长期目标是边缘设备部署")
        self.assertEqual(trait["status"], "active")
        self.assertEqual(trait["first_seen"], "2026-03-01")
        self.assertEqual(sorted(trait["evidence_memory_ids"]), [1, 2])

    async def test_add_entity_card_trait_rejects_foreign_evidence(self):
        conn = CardConnection({"description": "", "stable_traits": [], "snapshots": []}, owned_memory_ids=[1])
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.add_entity_card_trait(
                1, "特征", first_seen="2026-03-01", last_confirmed="2026-08-11",
                evidence_memory_ids=[1, 999],
            )
        self.assertIn("证据记忆不属于该实体", result["error"])

    async def test_add_entity_card_trait_rejects_invalid_date(self):
        conn = CardConnection({"description": "", "stable_traits": [], "snapshots": []}, owned_memory_ids=[1])
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.add_entity_card_trait(
                1, "特征", first_seen="2026-13-40", last_confirmed="2026-08-11",
                evidence_memory_ids=[1],
            )
        self.assertIn("日期无效", result["error"])

    async def test_add_entity_card_trait_has_no_cap(self):
        card = {"description": "", "stable_traits": [
            {"id": f"t{i}", "text": f"特征{i}", "status": "active",
             "first_seen": "2026-01-01", "last_confirmed": "2026-08-01",
             "evidence_memory_ids": [i], "evidence_message_ids": [], "origin": "confirmed"}
            for i in range(1, 6)
        ], "snapshots": []}
        conn = CardConnection(card, owned_memory_ids=[1, 2, 3, 4, 5, 6])
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.add_entity_card_trait(
                1, "第六条", first_seen="2026-03-01", last_confirmed="2026-08-11",
                evidence_memory_ids=[6],
            )
        self.assertEqual(result["status"], "ok")
        written = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual(len(written["stable_traits"]), 6)

    async def test_retire_entity_card_trait_keeps_evidence(self):
        card = {"description": "", "stable_traits": [
            {"id": "t1", "text": "特征", "status": "active",
             "first_seen": "2026-03-01", "last_confirmed": "2026-08-11",
             "evidence_memory_ids": [1], "evidence_message_ids": [], "origin": "confirmed"},
        ], "snapshots": []}
        conn = CardConnection(card)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.retire_entity_card_trait(1, "t1")
        self.assertEqual(result["status"], "ok")
        written = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual(written["stable_traits"][0]["status"], "retired")
        self.assertEqual(written["stable_traits"][0]["evidence_memory_ids"], [1])

    async def test_record_memory_evidence_skips_empty(self):
        conn = EvidenceConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            await database.record_memory_evidence(5, [])
        self.assertEqual(conn.executemany_calls, [])

    async def test_record_memory_evidence_links_user_messages(self):
        conn = EvidenceConnection()
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            await database.record_memory_evidence(5, [10, 11])
        self.assertEqual(len(conn.executemany_calls), 1)
        sql, rows = conn.executemany_calls[0]
        self.assertIn("INSERT INTO memory_evidence", sql)
        self.assertEqual(rows, [(5, 10, "user"), (5, 11, "user")])

    async def test_update_snapshot_edits_state_and_date(self):
        card = {
            "description": "",
            "snapshots": [
                {"fact_date": "2026-07-01", "recorded_at": "2026-07-01T00:00:00+00:00",
                 "state": "住在上海", "evidence_memory_id": 1, "evidence_message_id": 2, "source": "confirmed"},
            ],
        }
        conn = CardConnection(card)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.update_entity_card_snapshot(
                1, "2026-07-01T00:00:00+00:00", "搬到北京", "2026-07-20",
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(conn.execute_calls), 1)
        updated = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual(updated["snapshots"][0]["state"], "搬到北京")
        self.assertEqual(updated["snapshots"][0]["fact_date"], "2026-07-20")

    async def test_update_snapshot_sanitizes_user_references(self):
        card = {
            "description": "",
            "snapshots": [
                {"fact_date": "2026-07-01", "recorded_at": "2026-07-01T00:00:00+00:00",
                 "state": "住在上海", "source": "confirmed"},
            ],
        }
        conn = CardConnection(card)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.update_entity_card_snapshot(
                1, "2026-07-01T00:00:00+00:00", "用户在画画", None,
            )
        self.assertEqual(result["status"], "ok")
        updated = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual(updated["snapshots"][0]["state"], "晏晏在画画")

    async def test_update_snapshot_not_found(self):
        conn = CardConnection({"description": "", "snapshots": []})
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.update_entity_card_snapshot(1, "no-such", "x", "2026-07-20")
        self.assertIn("未找到", result["error"])

    async def test_delete_snapshot_removes_and_resorts(self):
        card = {
            "description": "",
            "snapshots": [
                {"fact_date": "2026-07-20", "recorded_at": "2026-07-20T00:00:00+00:00",
                 "state": "住在北京", "source": "confirmed"},
                {"fact_date": "2026-07-01", "recorded_at": "2026-07-01T00:00:00+00:00",
                 "state": "住在上海", "source": "confirmed"},
            ],
        }
        conn = CardConnection(card)
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.delete_entity_card_snapshot(1, "2026-07-01T00:00:00+00:00")
        self.assertEqual(result["status"], "ok")
        updated = json.loads(conn.execute_calls[0][1][1])
        self.assertEqual([s["state"] for s in updated["snapshots"]], ["住在北京"])

    async def test_delete_snapshot_not_found(self):
        conn = CardConnection({"description": "", "snapshots": []})
        with patch.object(database, "get_pool", AsyncMock(return_value=FakePool(conn))):
            result = await database.delete_entity_card_snapshot(1, "no-such")
        self.assertIn("未找到", result["error"])


if __name__ == "__main__":
    unittest.main()
