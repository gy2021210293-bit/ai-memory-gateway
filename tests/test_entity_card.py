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

    def __init__(self, card_json, entity_exists=True):
        self.card_json = card_json
        self.entity_exists = entity_exists
        self.execute_calls = []

    def transaction(self):
        return AsyncContext()

    async def fetchrow(self, _sql, *_args):
        if not self.entity_exists:
            return None
        return {"entity_card_json": self.card_json}

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class EvidenceConnection:
    def __init__(self):
        self.executemany_calls = []

    async def executemany(self, sql, rows):
        self.executemany_calls.append((sql, rows))


class EntityCardTests(unittest.TestCase):
    def test_parse_entity_card_empty_defaults(self):
        self.assertEqual(database._parse_entity_card(None), {"description": "", "snapshots": []})
        self.assertEqual(database._parse_entity_card("not json"), {"description": "", "snapshots": []})
        self.assertEqual(database._parse_entity_card([]), {"description": "", "snapshots": []})

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

    def test_sort_and_cap_snapshots_keeps_newest_six(self):
        snapshots = [
            {"fact_date": f"2026-01-{day:02d}", "recorded_at": "", "state": f"state-{day}", "source": "direct"}
            for day in range(1, 9)
        ]
        ordered = database._sort_and_cap_snapshots(snapshots)
        self.assertEqual(len(ordered), 6)
        self.assertEqual(ordered[-1]["state"], "state-8")
        self.assertEqual(ordered[0]["state"], "state-3")

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


if __name__ == "__main__":
    unittest.main()
